#pragma once

// Private building blocks for `--sampler candidates --candidates FILE` (format
// `pineforge_candidates_v1`). Nothing here is wired into the CLI yet: the integration lane
// owns main.cpp, CMake and Python. See docs/internal/methods-c-native.md for the contract,
// the error taxonomy and the exact hooks.
//
// Four independent pieces:
//   1. read_candidate_list_file / parse_candidate_list / load_candidate_list: bounded
//      regular-file admission of the whole list, before any plugin or dataset is touched.
//   2. CandidateList: immutable canonical vectors with source and list digests.
//   3. CandidateListCursor: ordered occurrence access, usable as an evaluate_batches
//      proposer. Duplicates are separate occurrences; trial id == zero-based position.
//   4. CandidateCoverage: terminal-position bitmap for the archive's existing lock.
//
// Diagnostics carry only the 1-based line number, a declared dimension name and fixed
// vocabulary. They never echo the path, a list key, or a list value.

#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/pruner.hpp>
#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/types.hpp>

#include "continuation.hpp"
#include "json.hpp"
#include "sha256.hpp"

#include <algorithm>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <iterator>
#include <map>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

namespace pineforge::hpo::detail {

inline constexpr std::string_view candidate_list_format = "pineforge_candidates_v1";
inline constexpr std::string_view candidate_list_implementation = "pineforge_candidate_list_v1";

// Admission maxima. They bound INPUT only: they do not bound output size, resident memory or
// whether a wide result can be re-imported by the warm-start loader.
inline constexpr std::uint64_t candidate_list_max_occurrences = 50'000;
inline constexpr std::uint64_t candidate_list_max_file_bytes = 32ULL * 1024 * 1024;
inline constexpr std::uint64_t candidate_list_max_line_bytes = 64ULL * 1024;

// Callers may only tighten the maxima (tests do); a larger value is an invariant violation.
struct CandidateListLimits {
    std::uint64_t max_occurrences = candidate_list_max_occurrences;
    std::uint64_t max_file_bytes = candidate_list_max_file_bytes;
    std::uint64_t max_line_bytes = candidate_list_max_line_bytes;
};

[[noreturn]] inline void candidate_list_input_error(const std::string& message) {
    throw TypedHpoError<std::runtime_error>("hpo_input_file_invalid", {}, message);
}

[[noreturn]] inline void candidate_list_study_error(const char* reason,
                                                     const std::string& message) {
    throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid", {{"reason", reason}},
                                               message);
}

[[noreturn]] inline void candidate_list_invariant(const std::string& message) {
    throw TypedHpoError<std::logic_error>("hpo_invariant", {}, message);
}

inline void validate_candidate_list_limits(const CandidateListLimits& limits) {
    if (limits.max_occurrences == 0 || limits.max_occurrences > candidate_list_max_occurrences ||
        limits.max_file_bytes == 0 || limits.max_file_bytes > candidate_list_max_file_bytes ||
        limits.max_line_bytes == 0 || limits.max_line_bytes > candidate_list_max_line_bytes)
        candidate_list_invariant("candidate list limits exceed the admission maxima");
}

// A fixed input that shares a name with a search dimension would be found per trial today;
// for a candidate list it is a configuration error found at admission.
inline void require_no_fixed_input_overlap(
    const SearchSpace& space, const std::map<std::string, std::string>& fixed_inputs) {
    for (const auto& entry : fixed_inputs) {
        if (space.find(entry.first) != nullptr)
            candidate_list_study_error("input",
                                       "fixed input overlaps search dimension: " + entry.first);
    }
}

// Strict UTF-8 (RFC 3629): no overlong forms, no surrogates, nothing above U+10FFFF.
inline bool candidate_list_valid_utf8(std::string_view text) noexcept {
    std::size_t index = 0;
    while (index < text.size()) {
        const auto lead = static_cast<unsigned char>(text[index]);
        if (lead < 0x80) {
            ++index;
            continue;
        }
        std::size_t tails = 0;
        std::uint32_t minimum = 0;
        std::uint32_t point = 0;
        if (lead >= 0xc2 && lead <= 0xdf) {
            tails = 1;
            minimum = 0x80;
            point = lead & 0x1fU;
        } else if (lead >= 0xe0 && lead <= 0xef) {
            tails = 2;
            minimum = 0x800;
            point = lead & 0x0fU;
        } else if (lead >= 0xf0 && lead <= 0xf4) {
            tails = 3;
            minimum = 0x10000;
            point = lead & 0x07U;
        } else {
            return false;
        }
        if (text.size() - index <= tails)
            return false;
        for (std::size_t tail = 1; tail <= tails; ++tail) {
            const auto next = static_cast<unsigned char>(text[index + tail]);
            if ((next & 0xc0U) != 0x80U)
                return false;
            point = (point << 6) | (next & 0x3fU);
        }
        if (point < minimum || point > 0x10ffffU || (point >= 0xd800U && point <= 0xdfffU))
            return false;
        index += tails + 1;
    }
    return true;
}

// Opens one path read-only and reads it once. The descriptor is inspected, not the path, so
// a swap between check and read cannot change what is validated. O_NONBLOCK keeps a FIFO
// from blocking open(); anything that is not a regular file is refused before any read.
// Symlinks are resolved by the OS; the identity of the input is the bytes read.
inline std::string read_candidate_list_file(const std::filesystem::path& path,
                                            const CandidateListLimits& limits = {}) {
    validate_candidate_list_limits(limits);
    const auto& native = path.native();
    if (native.empty())
        candidate_list_input_error("candidate list path is empty");
    if (native.find('\0') != std::string::npos)
        candidate_list_input_error("candidate list path is invalid");
    int flags = O_RDONLY | O_NONBLOCK;
#ifdef O_CLOEXEC
    flags |= O_CLOEXEC;
#endif
    const int descriptor = ::open(native.c_str(), flags);
    if (descriptor < 0)
        candidate_list_input_error("candidate list cannot be opened");
    struct Closer {
        int descriptor;
        ~Closer() { ::close(descriptor); }
    } closer{descriptor};
    struct stat status {};
    if (::fstat(descriptor, &status) != 0)
        candidate_list_input_error("candidate list cannot be inspected");
    if (!S_ISREG(status.st_mode))
        candidate_list_input_error("candidate list is not a regular file");
    const std::string too_large =
        "candidate list exceeds " + std::to_string(limits.max_file_bytes) + " bytes";
    if (status.st_size < 0 || static_cast<std::uint64_t>(status.st_size) > limits.max_file_bytes)
        candidate_list_input_error(too_large);
    std::string bytes;
    bytes.reserve(static_cast<std::size_t>(status.st_size));
    char buffer[65536];
    for (;;) {
        const auto count = ::read(descriptor, buffer, sizeof(buffer));
        if (count < 0) {
            if (errno == EINTR)
                continue;
            candidate_list_input_error("candidate list read failed");
        }
        if (count == 0)
            break;
        // A file that grows while it is read is bounded by the same cap.
        if (bytes.size() + static_cast<std::size_t>(count) > limits.max_file_bytes)
            candidate_list_input_error(too_large);
        bytes.append(buffer, static_cast<std::size_t>(count));
    }
    return bytes;
}

// Immutable canonical vectors in list order, stored flat (occurrence-major, dimension
// declaration order). Duplicates are kept as separate occurrences; occurrence ids are the
// zero-based positions. Safe to read from many threads.
class CandidateList final {
public:
    CandidateList(std::vector<std::string> names, std::vector<ParameterValue> values,
                  std::uint64_t count, std::string source_sha256)
        : names_(std::move(names)), values_(std::move(values)), count_(count),
          source_sha256_(std::move(source_sha256)) {
        if (count_ == 0 || values_.size() != count_ * names_.size())
            candidate_list_invariant("candidate list storage is inconsistent");
        // The digest is a pure function of the canonical vectors: the format line, then one
        // candidate_key line per occurrence in order. Key order, whitespace and number
        // spelling of the source file do not matter; duplicates and order do.
        std::string material(candidate_list_format);
        material += '\n';
        for (std::uint64_t position = 0; position < count_; ++position) {
            material += candidate_key(at(position));
            material += '\n';
        }
        list_sha256_ = sha256(material);
    }

    std::uint64_t size() const noexcept { return count_; }

    // The occurrence at `position`; Candidate::id is the position.
    Candidate at(std::uint64_t position) const {
        if (position >= count_)
            throw TypedHpoError<std::out_of_range>("hpo_invariant", {},
                                                   "candidate list position is out of range");
        Candidate candidate;
        candidate.id = position;
        const std::size_t width = names_.size();
        const std::size_t base = static_cast<std::size_t>(position) * width;
        for (std::size_t index = 0; index < width; ++index)
            candidate.values.emplace(names_[index], values_[base + index]);
        return candidate;
    }

    // SHA-256 of the exact source bytes.
    const std::string& source_sha256() const noexcept { return source_sha256_; }
    // SHA-256 of the ordered canonical list (see the constructor).
    const std::string& list_sha256() const noexcept { return list_sha256_; }

private:
    std::vector<std::string> names_;
    std::vector<ParameterValue> values_;
    std::uint64_t count_;
    std::string source_sha256_;
    std::string list_sha256_;
};

// Ordered proposer for evaluate_batches. propose() runs on the coordinator thread only, so
// the cursor is not synchronized. The list must outlive the cursor.
class CandidateListCursor final {
public:
    explicit CandidateListCursor(const CandidateList& list) : list_(&list) {}

    std::optional<Candidate> next() {
        if (position_ >= list_->size())
            return std::nullopt;
        return list_->at(position_++);
    }

    std::uint64_t position() const noexcept { return position_; }

private:
    const CandidateList* list_;
    std::uint64_t position_ = 0;
};

struct CandidateListContext {
    const SearchSpace& space;
    const CandidateListLimits& limits;
    // True when the space has a finite cardinality: vectors are then canonicalized through
    // candidate_ordinal/candidate_at exactly as the warm-start importer does.
    bool lattice;
};

inline const char* candidate_list_kind_word(DimensionKind kind) noexcept {
    switch (kind) {
    case DimensionKind::Integer: return "integer";
    case DimensionKind::Real: return "real";
    case DimensionKind::Boolean: return "boolean";
    case DimensionKind::Categorical: return "categorical choice";
    }
    return "valid value";
}

inline ParameterType candidate_list_expected_type(DimensionKind kind) noexcept {
    switch (kind) {
    case DimensionKind::Integer: return ParameterType::Integer;
    case DimensionKind::Real: return ParameterType::Real;
    case DimensionKind::Boolean: return ParameterType::Boolean;
    case DimensionKind::Categorical: break;
    }
    return ParameterType::String;
}

// The warm-start importer's scalar rules (continuation.hpp, load_json_warm_history): a real
// dimension takes any JSON number as a binary64; every other dimension takes the scalar its
// spelling denotes (no '.', 'e' or 'E' is an int64, otherwise a real); a categorical number
// that is not a declared choice may still match a declared real choice.
inline ParameterValue decode_candidate_list_value(const Dimension& dimension, const Json& value) {
    ParameterValue scalar = dimension_kind(dimension) == DimensionKind::Real
        ? ParameterValue(value.real()) : scalar_value(value);
    if (const auto* categorical = std::get_if<CategoricalDimension>(&dimension)) {
        if (value.kind == Json::Kind::Number && !categorical->contains(scalar)) {
            const ParameterValue real = value.real();
            if (categorical->contains(real))
                scalar = real;
        }
    }
    return scalar;
}

// Validates one line completely and returns its canonical vector in dimension order.
inline std::vector<ParameterValue> decode_candidate_list_line(
    const CandidateListContext& context, std::string_view line, std::uint64_t line_number) {
    const std::string where = "candidate list line " + std::to_string(line_number);
    if (!candidate_list_valid_utf8(line))
        candidate_list_input_error(where + " is not valid UTF-8");
    Json document;
    try {
        document = parse_json(line, static_cast<std::size_t>(context.limits.max_line_bytes));
    } catch (const std::bad_alloc&) {
        throw;
    } catch (const std::exception&) {
        // The parser's own text can name a duplicate key; replace it wholesale.
        candidate_list_input_error(where + " is not valid JSON");
    }
    if (document.kind != Json::Kind::Object)
        candidate_list_study_error("search_space", where + " must be a JSON object");
    const auto& dimensions = context.space.dimensions();
    Candidate candidate;
    for (const auto& dimension : dimensions) {
        const std::string name(dimension_name(dimension));
        const Json* member = document.find(name);
        if (member == nullptr)
            candidate_list_study_error("search_space", where + " is missing parameter " + name);
        const auto kind = dimension_kind(dimension);
        ParameterValue scalar;
        try {
            scalar = decode_candidate_list_value(dimension, *member);
        } catch (const std::bad_alloc&) {
            throw;
        } catch (const std::exception&) {
            candidate_list_study_error(
                "search_space", where + " parameter " + name + ": expected " +
                    candidate_list_kind_word(kind));
        }
        if (!dimension_contains(dimension, scalar)) {
            const bool wrong_type = kind != DimensionKind::Categorical &&
                parameter_type(scalar) != candidate_list_expected_type(kind);
            candidate_list_study_error(
                "search_space",
                where + " parameter " + name +
                    (wrong_type ? std::string(": expected ") + candidate_list_kind_word(kind)
                                : std::string(" is outside the declared domain")));
        }
        candidate.values.emplace(name, std::move(scalar));
    }
    // Every dimension is present and keys are unique, so a size mismatch means extra keys. A
    // key that names only a fixed input is one of them. The key is never echoed.
    if (document.members.size() != dimensions.size())
        candidate_list_study_error(
            "search_space", where + " has a parameter that is not a search dimension");
    if (!context.space.is_valid(candidate))
        candidate_list_study_error("search_space", where + " is not a valid search-space point");
    // A finite space is canonicalized through the lattice exactly as the importer does. A space
    // that is continuous (or too large to index) keeps every accepted scalar as decoded, sign
    // bit of a zero included: the digest of candidate_key treats -0.0 and +0.0 alike, and
    // source_sha256 is what distinguishes the original bytes.
    if (context.lattice) {
        try {
            candidate = context.space.candidate_at(context.space.candidate_ordinal(candidate), 0);
        } catch (const std::bad_alloc&) {
            throw;
        } catch (const std::exception&) {
            candidate_list_study_error(
                "search_space", where + " is not a canonical point of the finite search space");
        }
    }
    std::vector<ParameterValue> row;
    row.reserve(dimensions.size());
    for (const auto& dimension : dimensions)
        row.push_back(std::move(candidate.values.at(std::string(dimension_name(dimension)))));
    return row;
}

// Admits a whole list from memory. Checks run in this order, so the first failure is
// deterministic: limits, fixed-input overlap, empty, file size, then each line in file order
// (occurrence count, line length, UTF-8, JSON, vector against the space). Nothing is returned
// unless every line is valid. One final LF is allowed; blank lines, comments, a header and a
// BOM are not. A trailing CR is JSON whitespace, so CRLF files are accepted.
inline CandidateList parse_candidate_list(
    std::string bytes, const SearchSpace& space,
    const std::map<std::string, std::string>& fixed_inputs = {},
    const CandidateListLimits& limits = {}) {
    validate_candidate_list_limits(limits);
    require_no_fixed_input_overlap(space, fixed_inputs);
    if (bytes.empty())
        candidate_list_study_error("sampler", "candidate list is empty");
    if (bytes.size() > limits.max_file_bytes)
        candidate_list_input_error(
            "candidate list exceeds " + std::to_string(limits.max_file_bytes) + " bytes");
    bool lattice = false;
    try {
        lattice = space.finite_cardinality().has_value();
    } catch (const std::overflow_error&) {
        // Too large to index, as in the importer: validated but not canonicalized.
    }
    const CandidateListContext context{space, limits, lattice};
    std::vector<std::string> names;
    names.reserve(space.dimensions().size());
    for (const auto& dimension : space.dimensions())
        names.emplace_back(dimension_name(dimension));
    std::vector<ParameterValue> values;
    std::uint64_t count = 0;
    const std::string_view text(bytes);
    for (std::size_t begin = 0; begin < text.size();) {
        const std::size_t newline = text.find('\n', begin);
        const std::size_t stop = newline == std::string_view::npos ? text.size() : newline;
        const std::string_view line = text.substr(begin, stop - begin);
        const std::uint64_t line_number = count + 1;
        if (count >= limits.max_occurrences)
            candidate_list_study_error(
                "sampler",
                "candidate list exceeds " + std::to_string(limits.max_occurrences) +
                    " occurrences");
        if (line.size() > limits.max_line_bytes)
            candidate_list_input_error("candidate list line " + std::to_string(line_number) +
                                       " exceeds " + std::to_string(limits.max_line_bytes) +
                                       " bytes");
        auto row = decode_candidate_list_line(context, line, line_number);
        values.insert(values.end(), std::make_move_iterator(row.begin()),
                      std::make_move_iterator(row.end()));
        ++count;
        begin = stop + 1;
    }
    std::string source = sha256(text);
    // Release the raw bytes before the digest material is built; `text` dangles after this.
    std::string().swap(bytes);
    return CandidateList(std::move(names), std::move(values), count, std::move(source));
}

// Admission from a path. Configuration conflicts are reported before the file is touched.
inline CandidateList load_candidate_list(
    const std::filesystem::path& path, const SearchSpace& space,
    const std::map<std::string, std::string>& fixed_inputs = {},
    const CandidateListLimits& limits = {}) {
    validate_candidate_list_limits(limits);
    require_no_fixed_input_overlap(space, fixed_inputs);
    return parse_candidate_list(read_candidate_list_file(path, limits), space, fixed_inputs,
                                limits);
}

// An explicit trial budget (Options::max_trials, 0 when not given) must equal the list
// length; there is no prefix semantics.
inline void require_candidate_list_budget(std::uint64_t explicit_budget, std::uint64_t count) {
    if (explicit_budget != 0 && explicit_budget != count)
        candidate_list_study_error(
            "sampler", "the trial budget (" + std::to_string(explicit_budget) +
                           ") must be omitted or equal the candidate list length (" +
                           std::to_string(count) + ")");
}

[[noreturn]] inline void candidate_list_usage_error(const std::string& message) {
    throw TypedHpoError<std::invalid_argument>(
        "hpo_cli_usage", {}, message + "\nRun `pineforge-hpo-native --help` for usage.");
}

// Flag cross-checks for the candidates sampler. Other samplers are not affected: with
// `sampler_selected == false` only a stray `--candidates` is refused. Usage problems are
// `hpo_cli_usage` (exit 1); a warm start is `hpo_warm_start_rejected` (exit 4). Patience 0 is
// accepted, a positive patience is refused. `--seed` and `--trials-out` are unrestricted.
inline void require_candidate_list_settings(bool sampler_selected, bool file_given,
                                            CandidatePolicy candidate_policy,
                                            PrunerKind pruner,
                                            std::uint64_t no_improvement_trials,
                                            bool warm_start_given) {
    if (sampler_selected && !file_given)
        candidate_list_usage_error("--sampler candidates requires --candidates FILE");
    if (!sampler_selected && file_given)
        candidate_list_usage_error("--candidates requires --sampler candidates");
    if (!sampler_selected)
        return;
    if (candidate_policy != CandidatePolicy::SamplerDefault)
        candidate_list_usage_error(
            "--candidate-policy must be sampler_default with --sampler candidates");
    if (pruner != PrunerKind::None)
        candidate_list_usage_error("--pruner is not supported with --sampler candidates");
    if (no_improvement_trials != 0)
        candidate_list_usage_error(
            "--no-improvement-trials is not supported with --sampler candidates");
    if (warm_start_given)
        throw WarmStartError("candidates sampler continuation is not supported");
}

// Terminal-position coverage of one list. NOT synchronized: call record() under the lock
// that already guards the archive counters (TrialArchive::add is reached only with
// RunState's mutex held), and read it after RunState::shutdown() or once a timeout has
// frozen the archive. Memory is ceil(count / 64) words, at most 782 words for 50,000.
//
// Complete means every position has a terminal row of any status. It says nothing about
// whether those rows were scored; scored() counts ok and constraint_violation rows only.
class CandidateCoverage final {
public:
    using Range = std::pair<std::uint64_t, std::uint64_t>;  // inclusive [first, last]

    explicit CandidateCoverage(std::uint64_t count) : count_(count) {
        if (count == 0 || count > candidate_list_max_occurrences)
            candidate_list_invariant("candidate coverage size is outside the admission bound");
        words_.assign(static_cast<std::size_t>((count + 63) / 64), 0);
    }

    std::uint64_t count() const noexcept { return count_; }
    std::uint64_t evaluated() const noexcept { return evaluated_; }
    std::uint64_t scored() const noexcept { return scored_; }
    bool complete() const noexcept { return evaluated_ == count_; }

    bool terminal(std::uint64_t position) const noexcept {
        return position < count_ &&
               ((words_[static_cast<std::size_t>(position / 64)] >> (position % 64)) & 1U) != 0;
    }

    // Marks `position` terminal. A position outside the list or recorded twice is a bug in
    // the caller's id mapping and throws hpo_invariant instead of corrupting the claim.
    void record(std::uint64_t position, bool scored) {
        if (position >= count_)
            throw TypedHpoError<std::out_of_range>(
                "hpo_invariant", {}, "candidate coverage position is outside the list");
        auto& word = words_[static_cast<std::size_t>(position / 64)];
        const std::uint64_t bit = std::uint64_t{1} << (position % 64);
        if ((word & bit) != 0)
            candidate_list_invariant("candidate coverage position was recorded twice");
        word |= bit;
        ++evaluated_;
        if (scored)
            ++scored_;
    }

    // Maximal runs of positions with no terminal row, ascending and inclusive.
    std::vector<Range> missing_ranges() const {
        std::vector<Range> ranges;
        std::uint64_t position = 0;
        while (position < count_) {
            if (terminal(position)) {
                ++position;
                continue;
            }
            const std::uint64_t first = position;
            while (position < count_ && !terminal(position))
                ++position;
            ranges.emplace_back(first, position - 1);
        }
        return ranges;
    }

private:
    std::uint64_t count_;
    std::uint64_t evaluated_ = 0;
    std::uint64_t scored_ = 0;
    std::vector<std::uint64_t> words_;
};

// The value of the C-only top-level `candidate_list` key, on one line with no whitespace.
// The path is never part of it.
inline std::string candidate_list_json(const CandidateList& list,
                                       const CandidateCoverage& coverage) {
    if (coverage.count() != list.size())
        candidate_list_invariant("candidate coverage does not match the candidate list");
    std::string out = "{\"format\":\"";
    out += candidate_list_format;
    out += "\",\"source_sha256\":\"";
    out += list.source_sha256();
    out += "\",\"list_sha256\":\"";
    out += list.list_sha256();
    out += "\",\"count\":";
    out += std::to_string(list.size());
    out += ",\"evaluated\":";
    out += std::to_string(coverage.evaluated());
    out += ",\"scored\":";
    out += std::to_string(coverage.scored());
    out += ",\"complete\":";
    out += coverage.complete() ? "true" : "false";
    out += ",\"unevaluated_ranges\":[";
    bool first = true;
    for (const auto& range : coverage.missing_ranges()) {
        if (!first)
            out += ',';
        first = false;
        out += '[';
        out += std::to_string(range.first);
        out += ',';
        out += std::to_string(range.second);
        out += ']';
    }
    out += "]}";
    return out;
}

}  // namespace pineforge::hpo::detail
