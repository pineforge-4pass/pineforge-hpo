#pragma once

// Sobol descriptor, part accounting and parent admission (contract N9, N10, N12).
//
// STATUS: UNEXECUTED source preparation (methods-sobol-native leaf, base 1ed3402d). Nothing here has
// been compiled or run; the first compile and every test belong to the spot proof. The component is
// header-only and is NOT wired into main.cpp, CMake or the Python route: the final integrator owns
// the call sites listed in docs/internal/methods-sobol-native.md.
//
// What it owns
//   * SobolDescriptor: contract, table, word bits, scramble, seed, ordered columns, space hash,
//     numeric build identity, mapper contract and revision, and the identity hash over them.
//   * SobolPartAccumulator: constant-memory accounting of one part's terminal trial IDs, giving the
//     part's first_index, next_index and exact_stream even for best-k / none / cancelled / fatal
//     results whose retained rows are not the terminal set.
//   * admit_sobol_parent(): admission of a complete Sobol result as a continuation parent. It reuses
//     the generic loader for every generic validation and adds the Sobol checks on top.
//
// What it deliberately does not do
//   * It never derives or inspects numeric build flags. The numeric build identity is an input,
//     supplied and verified by the final integrator (null for discrete-only spaces, N11).
//   * It never edits or clones the generic warm loader, the canonical JSON or the space hash.
//   * It adds no checkpoint, REL31 or other persistent format; the descriptor lives in the result
//     JSON only.

#include "continuation.hpp"

#include "../core/sobol_engine.hpp"
#include "../core/sobol_mapper.hpp"
#include "../core/sobol_sampler.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <cstddef>
#include <filesystem>
#include <initializer_list>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace pineforge::hpo::detail {

/// Value of the `sobol.contract` field and of the identity input (N10).
inline constexpr std::string_view kSobolResultContract = "pineforge_sobol_v1";
/// Value of the result's `sampler_implementation` for the Sobol sampler (N1).
inline constexpr std::string_view kSobolImplementation = "pineforge_sobol_gray64_joe_kuo_d6_v1";
/// A numeric build identity containing this text has no actual-flags binding and cannot be continued (N11).
inline constexpr std::string_view kSobolFlagsUnavailable = "flags_sha256:unavailable";

/// Stable refusal texts. Every admission refusal is a WarmStartError (`hpo_warm_start_rejected`, exit 4)
/// whose text is one of these constants, optionally followed by a fixed field name. None interpolates
/// a path or a value taken from the parent document.
namespace sobol_refusal {
inline constexpr const char* kNotResult =
    "sobol parent must be a complete JSON result object with a trials array "
    "(rows-only, array and JSONL histories lose provenance)";
inline constexpr const char* kBinary = "sobol parent cannot be a binary history (no sobol provenance)";
inline constexpr const char* kUnreadable = "sobol parent file cannot be read or exceeds the 256 MiB cap";
inline constexpr const char* kForeignSampler = "sobol parent was not produced by the sobol sampler";
inline constexpr const char* kNotFullHistory = "sobol parent must retain all trials (trials_out all)";
inline constexpr const char* kBlockInvalid = "sobol parent has no valid sobol descriptor block";
inline constexpr const char* kFieldDiffers = "sobol parent descriptor field differs: ";
inline constexpr const char* kNumericUnavailable =
    "sobol parent numeric build identity has no flags binding and cannot be continued";
inline constexpr const char* kIdentityForged = "sobol parent identity does not match its descriptor";
inline constexpr const char* kIdentityMismatch = "sobol parent identity differs from this run";
inline constexpr const char* kNoRows = "sobol parent has no trials";
inline constexpr const char* kDuplicateId = "sobol parent has duplicate trial IDs";
inline constexpr const char* kCounts = "sobol parent trial counts contradict its rows";
inline constexpr const char* kIndices = "sobol parent first_index/next_index contradict its rows";
inline constexpr const char* kExactClaim = "sobol parent exact_stream claim contradicts its rows";
inline constexpr const char* kChanged = "sobol parent changed while it was being read";
inline constexpr const char* kRowForged =
    "sobol parent row does not match the sobol generator at its trial ID";
inline constexpr const char* kBudget = "new trial budget would overflow trial IDs";
}  // namespace sobol_refusal

[[noreturn]] inline void sobol_refuse(const std::string& message) { throw WarmStartError(message); }

/// Spelling of the scramble in the descriptor and on the command line (`--sobol-scramble`).
inline const char* sobol_scramble_name(SobolScramble scramble) noexcept {
    return scramble == SobolScramble::None ? "none" : "digital_shift";
}

/// Inverse of sobol_scramble_name(); std::nullopt for any other spelling.
inline std::optional<SobolScramble> sobol_scramble_from_name(std::string_view name) noexcept {
    if (name == "none")
        return SobolScramble::None;
    if (name == "digital_shift")
        return SobolScramble::DigitalShift;
    return std::nullopt;
}

/// Parses a canonical unsigned 64-bit decimal: digits only, no sign, no leading zeros (except "0"),
/// at most 20 digits, no overflow. Every 64-bit descriptor value travels as such a string.
inline std::optional<std::uint64_t> parse_sobol_decimal(std::string_view text) noexcept {
    if (text.empty() || text.size() > 20 || (text.size() > 1 && text.front() == '0'))
        return std::nullopt;
    std::uint64_t value = 0;
    for (const char character : text) {
        if (character < '0' || character > '9')
            return std::nullopt;
        const auto digit = static_cast<std::uint64_t>(character - '0');
        if (value > (std::numeric_limits<std::uint64_t>::max() - digit) / 10)
            return std::nullopt;
        value = value * 10 + digit;
    }
    return value;
}

inline bool sobol_is_sha256_hex(std::string_view text) noexcept {
    return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
               return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
           });
}

/// Everything that fixes a Sobol stream and its meaning (N10 without the per-part fields).
struct SobolDescriptor {
    std::string contract = std::string(kSobolResultContract);
    std::string implementation = std::string(kSobolImplementation);
    std::string table_name = kSobolTableName;
    std::string table_subset_sha256 = kSobolTableSubsetSha256;
    std::string table_upstream_sha256 = kSobolTableUpstreamSha256;
    std::uint32_t word_bits = kSobolWordBits;
    SobolScramble scramble = SobolScramble::DigitalShift;
    /// Digital-shift seed; std::nullopt for SobolScramble::None (the seed is then ignored, N4).
    std::optional<std::uint64_t> seed;
    /// Varying dimension names in column order (byte-wise UTF-8 order), as SobolSampler::columns().
    std::vector<std::string> columns;
    /// The result's space hash (64 lowercase hex), which already covers parameters and objective.
    std::string space_hash;
    /// Verified component supplied by the final integrator: std::nullopt exactly when no column uses
    /// binary64 math (N11). Never derived here.
    std::optional<std::string> numeric_build_identity;
    /// Mapper contract and revision (AR amendment 2026-10-09 14:10: bound in the identity).
    std::string mapper_contract = std::string(kSobolMapperContract);
    std::uint32_t mapper_revision = kSobolMapperRevision;
    /// SHA-256 (lowercase hex) of sobol_identity_input().
    std::string identity;
};

/// The canonical JSON the identity hash covers (keys in sorted order, 64-bit values as decimal
/// strings, no whitespace), per the space-hash v1 rules of canonical_space()/dump_json():
///   {"columns":[..],"contract":..,"implementation":..,"mapper_contract":..,"mapper_revision":N,
///    "numeric_build_identity":..|null,"scramble":..,"seed":".."|null,"space_hash":..,
///    "table_subset_sha256":..,"word_bits":64}
inline Json sobol_identity_json(const SobolDescriptor& descriptor) {
    auto input = object_json();
    auto columns = array_json();
    for (const auto& name : descriptor.columns)
        columns.items.push_back(Json::string(name));
    input.members["columns"] = std::move(columns);
    input.members["contract"] = Json::string(descriptor.contract);
    input.members["implementation"] = Json::string(descriptor.implementation);
    input.members["mapper_contract"] = Json::string(descriptor.mapper_contract);
    input.members["mapper_revision"] = Json::number(std::to_string(descriptor.mapper_revision));
    input.members["numeric_build_identity"] = descriptor.numeric_build_identity
        ? Json::string(*descriptor.numeric_build_identity) : Json{};
    input.members["scramble"] = Json::string(sobol_scramble_name(descriptor.scramble));
    input.members["seed"] = descriptor.seed ? Json::string(std::to_string(*descriptor.seed)) : Json{};
    input.members["space_hash"] = Json::string(descriptor.space_hash);
    input.members["table_subset_sha256"] = Json::string(descriptor.table_subset_sha256);
    input.members["word_bits"] = Json::number(std::to_string(descriptor.word_bits));
    return input;
}

/// Serialized identity input; exposed so tests and other implementations can recompute the hash.
inline std::string sobol_identity_input(const SobolDescriptor& descriptor) {
    return dump_json(sobol_identity_json(descriptor));
}

/// Recomputes the identity hash from the descriptor's fields (ignores descriptor.identity).
inline std::string sobol_identity(const SobolDescriptor& descriptor) {
    return sha256(sobol_identity_input(descriptor));
}

/// Builds the descriptor of a run from its sampler.
///
/// @param numeric_build_identity the verified Sobol numeric build identity (prefix portable-sobol-v1)
///        for a space with a stepped-real, linear-real, log-real or log-integer column; std::nullopt
///        for a discrete-only space. Passing it for a discrete-only space, or omitting it for a space
///        that needs it, is an integrator bug and throws hpo_invariant. The identity string is bound
///        verbatim and is never interpreted here, except that continuation refuses a string containing
///        kSobolFlagsUnavailable (see admit_sobol_parent()).
/// @throws TypedHpoError<std::logic_error> (hpo_invariant) on an integrator bug.
inline SobolDescriptor make_sobol_descriptor(const SobolSampler& sampler,
                                             std::string space_hash,
                                             std::optional<std::string> numeric_build_identity) {
    if (!sobol_is_sha256_hex(space_hash))
        throw TypedHpoError<std::logic_error>("hpo_invariant", {}, "sobol descriptor needs a space hash");
    const bool needs_numeric = sampler.uses_floating_point();
    if (needs_numeric != (numeric_build_identity.has_value() && !numeric_build_identity->empty()) ||
        (numeric_build_identity && numeric_build_identity->empty()))
        throw TypedHpoError<std::logic_error>(
            "hpo_invariant", {},
            "sobol numeric build identity must be supplied exactly when a column uses binary64 math");
    SobolDescriptor descriptor;
    descriptor.scramble = sampler.scramble();
    if (descriptor.scramble == SobolScramble::DigitalShift)
        descriptor.seed = sampler.seed();
    for (const auto& column : sampler.columns())
        descriptor.columns.push_back(column.name);
    descriptor.space_hash = std::move(space_hash);
    descriptor.numeric_build_identity = std::move(numeric_build_identity);
    descriptor.identity = sobol_identity(descriptor);
    return descriptor;
}

/// Per-part facts (N10 `first_index`, `next_index`, `exact_stream`).
struct SobolPartSummary {
    /// Index of the part's first candidate: the parent's next ID, or 0 for a fresh run.
    std::uint64_t first_index = 0;
    /// One more than the part's largest terminal trial ID; first_index when the part has none.
    std::uint64_t next_index = 0;
    /// Number of terminal trials of this part, whether or not their rows are retained.
    std::uint64_t terminal_trials = 0;
    /// True iff the parent rows (if any) are exactly IDs 0..first_index-1 and this part's terminal
    /// trials are exactly first_index..next_index-1. Empty ranges are vacuously exact.
    bool exact_stream = true;
};

/// Constant-memory accounting of one part's terminal trial IDs. Feed it EVERY terminal trial of the
/// part (each status: ok, errors, timeouts, pruned, ...), at the point where TrialArchive::add() sees
/// it, regardless of `trials_out`: retained rows (best-k, none) are not the terminal set. Not
/// thread-safe; call it under the archive's existing serialization.
class SobolPartAccumulator {
public:
    /// @throws TypedHpoError<std::logic_error> (hpo_invariant) for the reserved ID 2^64-1.
    void observe(std::uint64_t trial_id) {
        if (trial_id == std::numeric_limits<std::uint64_t>::max())
            throw TypedHpoError<std::logic_error>("hpo_invariant", {},
                                                  "sobol terminal trial ID 2^64-1 is reserved");
        if (count_ == 0) {
            minimum_ = maximum_ = trial_id;
        } else {
            minimum_ = std::min(minimum_, trial_id);
            maximum_ = std::max(maximum_, trial_id);
        }
        ++count_;
    }

    /// Number of terminal trials observed.
    std::uint64_t count() const noexcept { return count_; }

    /// @param first_index the part's first index (parent's next ID, 0 for a fresh run).
    /// @param parent_exact SobolAdmission::parent_exact of the admitted parent; true for a fresh run.
    /// @throws TypedHpoError<std::logic_error> (hpo_invariant) if an observed ID is below first_index or
    ///         an ID was observed twice (the terminal set must have unique IDs at or above first_index).
    SobolPartSummary summary(std::uint64_t first_index, bool parent_exact) const {
        SobolPartSummary result;
        result.first_index = first_index;
        result.terminal_trials = count_;
        if (count_ == 0) {
            result.next_index = first_index;
            result.exact_stream = parent_exact;
            return result;
        }
        if (minimum_ < first_index || count_ > maximum_ - minimum_ + 1)
            throw TypedHpoError<std::logic_error>(
                "hpo_invariant", {}, "sobol terminal trial IDs fall below first_index or repeat");
        result.next_index = maximum_ + 1;
        result.exact_stream = parent_exact && minimum_ == first_index && count_ == maximum_ - minimum_ + 1;
        return result;
    }

private:
    std::uint64_t count_ = 0;
    std::uint64_t minimum_ = 0;
    std::uint64_t maximum_ = 0;
};

/// The `sobol` block of a result (N10 plus `mapper`), keys sorted, 64-bit values as decimal strings.
inline Json sobol_block_json(const SobolDescriptor& descriptor, const SobolPartSummary& part) {
    auto block = object_json();
    block.members["contract"] = Json::string(descriptor.contract);
    auto table = object_json();
    table.members["name"] = Json::string(descriptor.table_name);
    table.members["subset_sha256"] = Json::string(descriptor.table_subset_sha256);
    table.members["upstream_sha256"] = Json::string(descriptor.table_upstream_sha256);
    block.members["table"] = std::move(table);
    block.members["word_bits"] = Json::number(std::to_string(descriptor.word_bits));
    block.members["scramble"] = Json::string(sobol_scramble_name(descriptor.scramble));
    block.members["seed"] = descriptor.seed ? Json::string(std::to_string(*descriptor.seed)) : Json{};
    auto columns = array_json();
    for (const auto& name : descriptor.columns)
        columns.items.push_back(Json::string(name));
    block.members["columns"] = std::move(columns);
    block.members["numeric_build_identity"] = descriptor.numeric_build_identity
        ? Json::string(*descriptor.numeric_build_identity) : Json{};
    auto mapper = object_json();
    mapper.members["contract"] = Json::string(descriptor.mapper_contract);
    mapper.members["revision"] = Json::number(std::to_string(descriptor.mapper_revision));
    block.members["mapper"] = std::move(mapper);
    block.members["identity"] = Json::string(descriptor.identity);
    block.members["first_index"] = Json::string(std::to_string(part.first_index));
    block.members["next_index"] = Json::string(std::to_string(part.next_index));
    block.members["exact_stream"] = Json::boolean(part.exact_stream);
    return block;
}

/// Text to embed as the value of the result's `"sobol"` key.
inline std::string render_sobol_block(const SobolDescriptor& descriptor, const SobolPartSummary& part) {
    return dump_json(sobol_block_json(descriptor, part));
}

/// Result of a successful admission.
struct SobolAdmission {
    /// The generic loader's validated history, ready for Options::warm_history.
    WarmHistory history;
    /// First index of the child run (== history.next_id): the largest history ID plus one.
    std::uint64_t next_id = 0;
    /// True iff the history rows (ancestors and the parent's own part) are exactly IDs 0..next_id-1.
    /// Feed it to SobolPartAccumulator::summary(). Derived from the rows, never from the parent's claim.
    bool parent_exact = false;
    /// Rows inherited from earlier parts (`warm_start_trials`) and rows of the parent's own part.
    std::uint64_t ancestor_rows = 0;
    std::uint64_t part_rows = 0;
};

namespace sobol_detail {

constexpr std::size_t kDocumentCap = 256U * 1024U * 1024U;  // the generic loader's cap

struct IdStats {
    std::uint64_t count = 0;
    std::uint64_t minimum = 0;
    std::uint64_t maximum = 0;
};

inline bool is_small_integer_text(const Json& value, std::uint32_t& out) {
    if (value.kind != Json::Kind::Number || value.value.empty() || value.value.size() > 9)
        return false;
    std::uint32_t parsed = 0;
    for (const char character : value.value) {
        if (character < '0' || character > '9')
            return false;
        parsed = parsed * 10 + static_cast<std::uint32_t>(character - '0');
    }
    out = parsed;
    return true;
}

inline bool has_exact_keys(const Json& object, std::initializer_list<const char*> keys) {
    if (object.kind != Json::Kind::Object || object.members.size() != keys.size())
        return false;
    for (const char* key : keys)
        if (object.members.find(key) == object.members.end())
            return false;
    return true;
}

inline bool string_member(const Json& object, const char* key, std::string& out) {
    const auto found = object.members.find(key);
    if (found == object.members.end() || found->second.kind != Json::Kind::String)
        return false;
    out = found->second.value;
    return true;
}

/// A parsed `sobol` block; every field already shape-checked, none yet compared to a run.
struct ParsedBlock {
    SobolDescriptor descriptor;
    std::uint64_t first_index = 0;
    std::uint64_t next_index = 0;
    bool exact_stream = false;
};

inline ParsedBlock parse_block(const Json& block) {
    using namespace sobol_refusal;
    if (!has_exact_keys(block, {"columns", "contract", "exact_stream", "first_index", "identity", "mapper",
                                "next_index", "numeric_build_identity", "scramble", "seed", "table",
                                "word_bits"}))
        sobol_refuse(kBlockInvalid);
    ParsedBlock parsed;
    auto& descriptor = parsed.descriptor;
    const auto& members = block.members;
    std::string scramble_name;
    if (!string_member(block, "contract", descriptor.contract) ||
        !string_member(block, "identity", descriptor.identity) ||
        !string_member(block, "scramble", scramble_name))
        sobol_refuse(kBlockInvalid);
    const auto scramble = sobol_scramble_from_name(scramble_name);
    if (!scramble)
        sobol_refuse(kBlockInvalid);
    descriptor.scramble = *scramble;
    if (!sobol_is_sha256_hex(descriptor.identity))
        sobol_refuse(kBlockInvalid);

    const auto& table = members.at("table");
    if (!has_exact_keys(table, {"name", "subset_sha256", "upstream_sha256"}) ||
        !string_member(table, "name", descriptor.table_name) ||
        !string_member(table, "subset_sha256", descriptor.table_subset_sha256) ||
        !string_member(table, "upstream_sha256", descriptor.table_upstream_sha256))
        sobol_refuse(kBlockInvalid);

    const auto& mapper = members.at("mapper");
    if (!has_exact_keys(mapper, {"contract", "revision"}) ||
        !string_member(mapper, "contract", descriptor.mapper_contract) ||
        !is_small_integer_text(mapper.members.at("revision"), descriptor.mapper_revision) ||
        !is_small_integer_text(members.at("word_bits"), descriptor.word_bits))
        sobol_refuse(kBlockInvalid);

    const auto& seed = members.at("seed");
    if (seed.kind == Json::Kind::String) {
        const auto value = parse_sobol_decimal(seed.value);
        if (!value)
            sobol_refuse(kBlockInvalid);
        descriptor.seed = *value;
    } else if (seed.kind != Json::Kind::Null) {
        sobol_refuse(kBlockInvalid);
    }

    const auto& columns = members.at("columns");
    if (columns.kind != Json::Kind::Array)
        sobol_refuse(kBlockInvalid);
    for (const auto& column : columns.items) {
        if (column.kind != Json::Kind::String)
            sobol_refuse(kBlockInvalid);
        descriptor.columns.push_back(column.value);
    }

    const auto& numeric = members.at("numeric_build_identity");
    if (numeric.kind == Json::Kind::String)
        descriptor.numeric_build_identity = numeric.value;
    else if (numeric.kind != Json::Kind::Null)
        sobol_refuse(kBlockInvalid);

    const auto& first = members.at("first_index");
    const auto& next = members.at("next_index");
    const auto& exact = members.at("exact_stream");
    if (first.kind != Json::Kind::String || next.kind != Json::Kind::String ||
        exact.kind != Json::Kind::Bool)
        sobol_refuse(kBlockInvalid);
    const auto first_value = parse_sobol_decimal(first.value);
    const auto next_value = parse_sobol_decimal(next.value);
    if (!first_value || !next_value)
        sobol_refuse(kBlockInvalid);
    parsed.first_index = *first_value;
    parsed.next_index = *next_value;
    parsed.exact_stream = exact.value == "true";
    return parsed;
}

/// Bitwise parameter equality: same alternative, doubles by bit pattern (so -0.0 != 0.0).
inline bool same_parameter(const ParameterValue& left, const ParameterValue& right) noexcept {
    if (left.index() != right.index())
        return false;
    if (const auto* real = std::get_if<double>(&left)) {
        std::uint64_t left_bits = 0;
        std::uint64_t right_bits = 0;
        std::memcpy(&left_bits, real, sizeof(left_bits));
        std::memcpy(&right_bits, &std::get<double>(right), sizeof(right_bits));
        return left_bits == right_bits;
    }
    return left == right;
}

inline bool same_point(const Candidate& left, const Candidate& right) noexcept {
    if (left.values.size() != right.values.size())
        return false;
    for (const auto& [name, value] : left.values) {
        const auto found = right.values.find(name);
        if (found == right.values.end() || !same_parameter(value, found->second))
            return false;
    }
    return true;
}

}  // namespace sobol_detail

/// Admits a complete Sobol result as the parent of a continuation (N9, N12).
///
/// Accepted: a JSON object (the full-result form; rows-only arrays, JSONL, binary v2, foreign-sampler
/// and summary / best-k / none results are refused, with no fallback) with sampler "sobol",
/// trials_out "all", an exact-key `sobol` block, the same descriptor and identity as @p expected,
/// consistent counts and indices, and every row (ancestors from `warm_start_trials` plus the part's
/// `trials`) equal to SobolSampler::at(trial_id) under the descriptor.
///
/// Order, cheap before heavy: (1) format and sampler gates; (2) descriptor field and identity
/// comparison, recomputing the parent's identity hash; (3) ID extraction, duplicate, partition, count,
/// index and exact_stream checks against the rows; (4) the generic loader (space/objective/symbol-feed
/// identity, statuses, parameter validity and canonicalization, the same 256 MiB caps, uint64 ID
/// rules); (5) a content-digest guard because the loader re-reads the file; (6) row-by-row generator
/// agreement; (7) the trial-ID budget. Nothing is trusted from `identity` or `exact_stream`: both are
/// recomputed and any contradiction is a refusal.
///
/// Gapped, cancelled and timed-out parents are admitted with point-by-ID meaning and
/// parent_exact == false; the child's first index is always the largest history ID plus one, so lower
/// holes are never reissued, reseeded or rejected.
///
/// @param expected the run's descriptor (make_sobol_descriptor()).
/// @param max_trials the run's requested budget (0 = limited only by wall time / available IDs).
/// @throws WarmStartError (`hpo_warm_start_rejected`, exit 4) for every refusal; messages are the
///         sobol_refusal constants or the generic loader's own diagnostics.
inline SobolAdmission admit_sobol_parent(const std::filesystem::path& path,
                                         const SearchSpace& space,
                                         const Json& recorded_space,
                                         const SobolDescriptor& expected,
                                         std::uint64_t max_trials) {
    using namespace sobol_refusal;
    using namespace sobol_detail;
    try {
        if (expected.identity != sobol_identity(expected))
            throw TypedHpoError<std::logic_error>("hpo_invariant", {},
                                                  "sobol descriptor identity is stale");

        // (1) Format and sampler gates.
        std::string content;
        try {
            content = read_document(path);
        } catch (const std::exception&) {
            sobol_refuse(kUnreadable);
        }
        const std::string content_digest = sha256(content);
        if (!content.empty() && content.front() == 'P')  // the generic dispatcher's binary test
            sobol_refuse(kBinary);
        std::optional<Json> parsed;
        try {
            parsed = parse_json(content, kDocumentCap);
        } catch (const std::exception&) {
            sobol_refuse(kNotResult);
        }
        content = std::string();
        const Json& document = *parsed;
        if (document.kind != Json::Kind::Object)
            sobol_refuse(kNotResult);
        const Json* trials = document.find("trials");
        if (!trials || trials->kind != Json::Kind::Array)
            sobol_refuse(kNotResult);
        const Json* sampler = document.find("sampler");
        if (!sampler || sampler->kind != Json::Kind::String || sampler->value != "sobol")
            sobol_refuse(kForeignSampler);
        const Json* mode = document.find("trials_out");
        if (!mode || mode->kind != Json::Kind::String || mode->value != "all")
            sobol_refuse(kNotFullHistory);

        // (2) Descriptor and identity.
        const Json* block = document.find("sobol");
        if (!block)
            sobol_refuse(kBlockInvalid);
        auto block_facts = parse_block(*block);
        auto& parent = block_facts.descriptor;
        const Json* implementation = document.find("sampler_implementation");
        const Json* hash = document.find("space_hash");
        const Json* hash_version = document.find("space_hash_version");
        if (!implementation || implementation->kind != Json::Kind::String)
            sobol_refuse(std::string(kFieldDiffers) + "implementation");
        if (!hash || hash->kind != Json::Kind::String || !hash_version ||
            hash_version->kind != Json::Kind::Number ||
            hash_version->value != std::to_string(space_hash_version))
            sobol_refuse(std::string(kFieldDiffers) + "space_hash");
        parent.implementation = implementation->value;
        parent.space_hash = hash->value;
        const auto differs = [](const char* name) { sobol_refuse(std::string(kFieldDiffers) + name); };
        if (parent.contract != expected.contract) differs("contract");
        if (parent.implementation != expected.implementation) differs("implementation");
        if (parent.table_name != expected.table_name || parent.table_subset_sha256 != expected.table_subset_sha256 ||
            parent.table_upstream_sha256 != expected.table_upstream_sha256) differs("table");
        if (parent.word_bits != expected.word_bits) differs("word_bits");
        if (parent.scramble != expected.scramble) differs("scramble");
        if (parent.seed != expected.seed) differs("seed");
        if (parent.columns != expected.columns) differs("columns");
        if (parent.space_hash != expected.space_hash) differs("space_hash");
        if (parent.numeric_build_identity != expected.numeric_build_identity) differs("numeric_build_identity");
        if (parent.mapper_contract != expected.mapper_contract || parent.mapper_revision != expected.mapper_revision)
            differs("mapper");
        if (expected.scramble == SobolScramble::DigitalShift) {
            // The result also prints the seed as a number; it must agree with the descriptor.
            const Json* top_seed = document.find("seed");
            if (!top_seed || top_seed->kind != Json::Kind::Number ||
                top_seed->value != std::to_string(*expected.seed))
                differs("seed");
        }
        if (expected.numeric_build_identity &&
            expected.numeric_build_identity->find(kSobolFlagsUnavailable) != std::string::npos)
            sobol_refuse(kNumericUnavailable);
        if (parent.identity != sobol_identity(parent))
            sobol_refuse(kIdentityForged);
        if (parent.identity != expected.identity)
            sobol_refuse(kIdentityMismatch);

        // (3) Rows, partition, counts and indices.
        const Json* inherited = document.find("warm_start_trials");
        const Json* warm = document.find("warm_start");
        if (inherited && inherited->kind != Json::Kind::Array)
            sobol_refuse(kCounts);
        const std::uint64_t inherited_count = inherited ? inherited->items.size() : 0;
        if (warm) {
            if (warm->kind != Json::Kind::Object ||
                json_integer(field(*warm, "trials")) != static_cast<std::int64_t>(inherited_count))
                sobol_refuse(kCounts);
        } else if (inherited_count != 0) {
            sobol_refuse(kCounts);
        }
        if (json_integer(field(document, "trials_completed")) != static_cast<std::int64_t>(trials->items.size()))
            sobol_refuse(kCounts);
        if (inherited_count + trials->items.size() == 0)
            sobol_refuse(kNoRows);

        std::vector<std::uint64_t> all_ids;
        all_ids.reserve(static_cast<std::size_t>(inherited_count + trials->items.size()));
        const auto collect = [&](const Json* rows) {
            IdStats stats;
            if (!rows)
                return stats;
            for (const auto& row : rows->items) {
                const std::uint64_t id = json_id(field(row, "trial_id"));  // refuses 2^64-1
                if (stats.count == 0)
                    stats.minimum = stats.maximum = id;
                else {
                    stats.minimum = std::min(stats.minimum, id);
                    stats.maximum = std::max(stats.maximum, id);
                }
                ++stats.count;
                all_ids.push_back(id);
            }
            return stats;
        };
        const IdStats ancestors = collect(inherited);
        const IdStats part = collect(trials);
        {
            auto sorted = all_ids;
            std::sort(sorted.begin(), sorted.end());
            if (std::adjacent_find(sorted.begin(), sorted.end()) != sorted.end())
                sobol_refuse(kDuplicateId);
        }
        const std::uint64_t first_index = ancestors.count == 0 ? 0 : ancestors.maximum + 1;
        if (block_facts.first_index != first_index)
            sobol_refuse(kIndices);
        if (part.count != 0 && part.minimum < first_index)
            sobol_refuse(kIndices);
        const std::uint64_t next_index = part.count == 0 ? first_index : part.maximum + 1;
        if (block_facts.next_index != next_index)
            sobol_refuse(kIndices);
        const bool ancestors_exact = ancestors.count == 0 ||
            (ancestors.minimum == 0 && ancestors.count == first_index);
        const bool part_exact = part.count == 0 ||
            (part.minimum == first_index && part.count == next_index - first_index);
        const bool exact = ancestors_exact && part_exact;
        if (block_facts.exact_stream != exact)
            sobol_refuse(kExactClaim);

        // (4) Generic validation (space, objective, symbol feeds, statuses, parameters, caps).
        WarmHistory history = load_json_warm_history(path, space, recorded_space);

        // (5) The loader re-read the file: it must be the bytes validated above.
        if (history.source_sha256 != content_digest)
            sobol_refuse(kChanged);
        if (history.binary || history.observations.size() != all_ids.size() ||
            history.records.size() != all_ids.size() || history.next_id != next_index)
            sobol_refuse(kCounts);

        // (6) Generator agreement for every row, ancestors included.
        const SobolSampler verifier(space, expected.seed.value_or(0), expected.scramble);
        for (const auto& observation : history.observations)
            if (!same_point(observation.candidate, verifier.at(observation.candidate.id)))
                sobol_refuse(kRowForged);

        // (7) Trial-ID budget (same rule and text as the generic preflight).
        if (history.next_id == std::numeric_limits<std::uint64_t>::max() ||
            max_trials > std::numeric_limits<std::uint64_t>::max() - history.next_id)
            sobol_refuse(kBudget);

        SobolAdmission admission;
        admission.next_id = history.next_id;
        admission.parent_exact = exact;
        admission.ancestor_rows = ancestors.count;
        admission.part_rows = part.count;
        admission.history = std::move(history);
        return admission;
    } catch (const WarmStartError&) {
        throw;
    } catch (const TypedHpoError<std::logic_error>&) {
        throw;  // integrator bugs keep hpo_invariant
    } catch (const std::exception& error) {
        throw WarmStartError(error.what());
    }
}

}  // namespace pineforge::hpo::detail
