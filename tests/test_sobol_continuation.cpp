// Sobol descriptor, part accounting and parent admission: unit tests (src/cli/sobol_continuation.hpp).
//
// STATUS: UNEXECUTED. Written test-first by the methods-sobol-native leaf (base 1ed3402d) under a rule
// that forbids compiling or running anything on the preparing machine. The first compile and run
// belong to the spot proof; until then nothing here is evidence of behaviour.
//
// Build notes for the integrator (this file is not registered in tests/CMakeLists.txt):
//   - include directories: <repo>/src/cli, <repo>/src/core and <repo>/include (like test_warm_binary);
//   - link PineForgeHPO::core (sobol_engine.cpp, sobol_mapper.cpp, sobol_sampler.cpp must be in it);
//   - compile options -Wall -Wextra -Wpedantic -ffp-contract=off -fno-fast-math;
//   - compile definition PFH_SOBOL_CONTINUATION_FIXTURES="<repo>/tests/fixtures/sobol/continuation".
//
// Independence: the static fixtures (space, identity inputs, a complete parent) were written by hand
// and hashed with `shasum -a 256`, never with the code under test, so they pin the documented canonical
// shapes. Everything else builds parents through the production descriptor and accumulator and then
// mutates or forges them; the row values always come from SobolSampler::at(), whose own correctness is
// proven by test_sobol_engine.cpp and test_sobol_mapper.cpp.
#include "sobol_continuation.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <optional>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#ifndef PFH_SOBOL_CONTINUATION_FIXTURES
#error "PFH_SOBOL_CONTINUATION_FIXTURES must name tests/fixtures/sobol/continuation"
#endif

namespace {

using namespace pineforge::hpo;
using namespace pineforge::hpo::detail;
namespace refusal = pineforge::hpo::detail::sobol_refusal;

constexpr std::uint64_t kMax = std::numeric_limits<std::uint64_t>::max();
const char* const kNumeric = "portable-sobol-v1;test-build:1";

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

std::string read_file(const std::string& name) {
    std::ifstream input(std::string(PFH_SOBOL_CONTINUATION_FIXTURES) + "/" + name, std::ios::binary);
    require(input.good(), "cannot open fixture " + name);
    std::ostringstream text;
    text << input.rdbuf();
    return text.str();
}

// Scratch directory for temporary parent files; removed at the end of main().
struct Scratch {
    std::filesystem::path dir;
    std::size_t counter = 0;
    Scratch() {
        dir = std::filesystem::temp_directory_path() /
              ("pf-sobol-continuation-" +
               std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        std::filesystem::create_directories(dir);
    }
    ~Scratch() {
        std::error_code ignored;
        std::filesystem::remove_all(dir, ignored);
    }
    std::filesystem::path write(const std::string& text) {
        const auto path = dir / ("parent-" + std::to_string(counter++) + ".json");
        std::ofstream output(path, std::ios::binary | std::ios::trunc);
        output << text;
        output.flush();
        require(output.good(), "cannot write a scratch parent");
        return path;
    }
};
Scratch* g_scratch = nullptr;

// A refusal must be the stable WarmStartError (hpo_warm_start_rejected), contain `expected`, and leak
// neither the scratch path nor any of the forged `forbidden` fragments.
void require_refused(const std::function<void()>& action,
                     const std::string& expected,
                     const std::string& what,
                     const std::vector<std::string>& forbidden = {}) {
    try {
        action();
    } catch (const WarmStartError& error) {
        const std::string message = error.what();
        require(error.code() == "hpo_warm_start_rejected", what + ": wrong failure code " + error.code());
        require(message.find(expected) != std::string::npos,
                what + ": unexpected message \"" + message + "\", wanted \"" + expected + "\"");
        require(message.find(g_scratch->dir.string()) == std::string::npos, what + ": the message leaks the path");
        for (const std::string& fragment : forbidden)
            require(message.find(fragment) == std::string::npos, what + ": the message leaks a parent value");
        return;
    } catch (const std::exception& error) {
        throw std::runtime_error(what + ": not a WarmStartError: " + error.what());
    }
    throw std::runtime_error(what + ": no refusal was thrown");
}

// ---- a producer for parents ---------------------------------------------------------------------

struct Env {
    SearchSpace space;
    Json recorded;
    std::string hash;
    SobolSampler sampler;
    SobolDescriptor descriptor;
    Env(SearchSpace s, SobolScramble scramble, std::uint64_t seed, std::optional<std::string> numeric)
        : space(std::move(s)),
          recorded(recorded_space(space, "net_profit", "maximize", {})),
          hash(space_hash(recorded)),
          sampler(space, seed, scramble),
          descriptor(make_sobol_descriptor(sampler, hash, std::move(numeric))) {}
    std::uint64_t seed() const { return sampler.seed(); }
};

SearchSpace k8_space() { return SearchSpace({IntegerDimension("k", 0, 7)}); }

// Discrete only, with two columns and a constant.
SearchSpace discrete_space() {
    return SearchSpace({IntegerDimension("alpha", -50, 50), BooleanDimension("flag"),
                        CategoricalDimension("kind", {std::string("ema"), std::string("sma"), std::string("wma")}),
                        IntegerDimension("pinned", 9, 9)});
}

// Every floating kind; needs a numeric build identity.
SearchSpace mixed_space() {
    return SearchSpace({IntegerDimension("alpha", -50, 50), BooleanDimension("flag"),
                        RealDimension("risk", -1.5, 2.5), RealDimension("grid", 0.0, 1.0, 0.125),
                        RealDimension("rate", 1e-3, 100.0, std::nullopt, true),
                        IntegerDimension("period", 2, 5000, 1, true)});
}

Json row_json(const Candidate& candidate) {
    Json row = object_json();
    row.members["trial_id"] = Json::number(std::to_string(candidate.id));
    row.members["status"] = Json::string("ok");
    row.members["feasible"] = Json::boolean(true);
    row.members["objective"] = Json::number(std::to_string(candidate.id % 97) + ".5");
    Json parameters = object_json();
    for (const auto& [name, value] : candidate.values)
        parameters.members[name] = scalar_json(value);
    row.members["parameters"] = std::move(parameters);
    return row;
}

bool ids_are_exact_prefix(const std::vector<std::uint64_t>& ids) {
    if (ids.empty())
        return true;
    const auto minimum = *std::min_element(ids.begin(), ids.end());
    const auto maximum = *std::max_element(ids.begin(), ids.end());
    return minimum == 0 && ids.size() == maximum + 1;
}

Json assemble(const Env& env, Json ancestors, std::uint64_t ancestor_count, std::uint64_t first_index,
              bool parent_exact, const std::vector<std::uint64_t>& part_ids) {
    SobolPartAccumulator accumulator;
    Json trials = array_json();
    for (const auto id : part_ids) {
        accumulator.observe(id);
        trials.items.push_back(row_json(env.sampler.at(id)));
    }
    const auto summary = accumulator.summary(first_index, parent_exact);
    Json document = object_json();
    document.members["schema_version"] = Json::number("1");
    document.members["space_hash_version"] = Json::number(std::to_string(space_hash_version));
    document.members["space_hash"] = Json::string(env.hash);
    document.members["space"] = env.recorded;
    document.members["sampler_implementation"] = Json::string(std::string(kSobolImplementation));
    document.members["sampler"] = Json::string("sobol");
    document.members["seed"] = Json::number(std::to_string(env.seed()));
    document.members["trials_completed"] = Json::number(std::to_string(part_ids.size()));
    document.members["trials"] = std::move(trials);
    document.members["trials_out"] = Json::string("all");
    document.members["best_k"] = Json::number("10");
    document.members["sobol"] = sobol_block_json(env.descriptor, summary);
    if (ancestor_count != 0) {
        Json warm = object_json();
        warm.members["trials"] = Json::number(std::to_string(ancestor_count));
        warm.members["completed"] = Json::number(std::to_string(ancestor_count));
        warm.members["feasible"] = Json::number(std::to_string(ancestor_count));
        warm.members["source_sha256"] = Json::string(std::string(64, '0'));
        warm.members["space_hash"] = Json::string(env.hash);
        document.members["warm_start"] = std::move(warm);
        document.members["warm_start_trials"] = std::move(ancestors);
    }
    return document;
}

// A document with synthetic ancestors (rows generated at the given IDs) and a part.
Json make_document(const Env& env, const std::vector<std::uint64_t>& ancestor_ids,
                   const std::vector<std::uint64_t>& part_ids) {
    Json ancestors = array_json();
    for (const auto id : ancestor_ids)
        ancestors.items.push_back(row_json(env.sampler.at(id)));
    const std::uint64_t first_index =
        ancestor_ids.empty() ? 0 : *std::max_element(ancestor_ids.begin(), ancestor_ids.end()) + 1;
    return assemble(env, std::move(ancestors), ancestor_ids.size(), first_index,
                    ids_are_exact_prefix(ancestor_ids), part_ids);
}

// A continuation document the way main.cpp would write it: ancestors are the admitted history.
Json make_child_document(const Env& env, const SobolAdmission& parent,
                         const std::vector<std::uint64_t>& part_ids) {
    Json ancestors = array_json();
    for (const auto& record : parent.history.records)
        ancestors.items.push_back(record);
    return assemble(env, std::move(ancestors), parent.history.records.size(), parent.next_id,
                    parent.parent_exact, part_ids);
}

std::vector<std::uint64_t> range(std::uint64_t first, std::uint64_t count) {
    std::vector<std::uint64_t> ids;
    for (std::uint64_t i = 0; i < count; ++i)
        ids.push_back(first + i);
    return ids;
}

SobolAdmission admit(const Env& env, const Json& document, std::uint64_t budget = 10) {
    return admit_sobol_parent(g_scratch->write(dump_json(document)), env.space, env.recorded, env.descriptor,
                              budget);
}

void expect_refusal(const Env& env, const Json& document, const std::string& expected, const std::string& what,
                    const std::vector<std::string>& forbidden = {}, std::uint64_t budget = 10) {
    const auto path = g_scratch->write(dump_json(document));
    require_refused([&] { (void)admit_sobol_parent(path, env.space, env.recorded, env.descriptor, budget); },
                    expected, what, forbidden);
}

Json edited(Json document, const std::function<void(Json&)>& edit) {
    edit(document);
    return document;
}

std::string field_differs(const char* name) { return std::string(refusal::kFieldDiffers) + name; }

// ---- constants, names, decimals ------------------------------------------------------------------

void test_constants_and_decimal_strings() {
    require(kSobolResultContract == "pineforge_sobol_v1", "result contract");
    require(kSobolImplementation == "pineforge_sobol_gray64_joe_kuo_d6_v1", "implementation name");
    require(kSobolFlagsUnavailable == "flags_sha256:unavailable", "flags marker");
    // The pin's table hashes, written out independently of the engine's constants.
    require(std::string(kSobolTableName) == "new-joe-kuo-6.21201", "table name");
    require(std::string(kSobolTableSubsetSha256) ==
                "52eeb57738dd69f5e1f593f2085c4efb3fb410c3af241d5396d7a2bef60b9257", "subset hash");
    require(std::string(kSobolTableUpstreamSha256) ==
                "68eedd2a4e3b659b9695e7aff0f8ac68718bcf620730fc3d3a8c65df2a067441", "upstream hash");
    require(std::string(sobol_scramble_name(SobolScramble::None)) == "none", "none spelling");
    require(std::string(sobol_scramble_name(SobolScramble::DigitalShift)) == "digital_shift", "shift spelling");
    require(sobol_scramble_from_name("none") == SobolScramble::None, "parse none");
    require(sobol_scramble_from_name("digital_shift") == SobolScramble::DigitalShift, "parse shift");
    for (const char* bad : {"", "None", "digital-shift", "digital_shift ", "owen"})
        require(!sobol_scramble_from_name(bad).has_value(), std::string("accepted scramble \"") + bad + "\"");

    require(parse_sobol_decimal("0") == 0ULL, "decimal 0");
    require(parse_sobol_decimal("9007199254740993") == 9007199254740993ULL, "decimal 2^53+1 is exact");
    require(parse_sobol_decimal("18446744073709551615") == kMax, "decimal 2^64-1");
    for (const char* bad : {"", "00", "01", "+1", "-1", " 1", "1 ", "1e3", "1.0", "0x10", "18446744073709551616",
                            "99999999999999999999", "184467440737095516150", "\xD9\xA3"})
        require(!parse_sobol_decimal(bad).has_value(), std::string("accepted decimal \"") + bad + "\"");
}

// ---- descriptor and identity ---------------------------------------------------------------------

void test_identity_matches_independent_fixtures() {
    // The recorded space and its canonical text were written by hand; the hashes come from shasum.
    const Env none(k8_space(), SobolScramble::None, 0, std::nullopt);
    require(dump_json(none.recorded) == read_file("space-k8.recorded.json"), "recorded space shape");
    require(canonical_space(none.recorded) == read_file("space-k8.canonical.txt"), "canonical space text");
    require(none.hash == read_file("space-k8.space_hash.txt"), "space hash");
    require(sha256(read_file("space-k8.canonical.txt")) == read_file("space-k8.space_hash.txt"),
            "sha256() disagrees with shasum on the canonical space");

    require(sobol_identity_input(none.descriptor) == read_file("identity-input-k8-none.txt"),
            "identity input (none) differs from the hand-written canonical text");
    require(none.descriptor.identity == read_file("identity-input-k8-none.sha256"), "identity (none)");
    require(sha256(read_file("identity-input-k8-none.txt")) == read_file("identity-input-k8-none.sha256"),
            "sha256() disagrees with shasum on the identity input");

    const Env shifted(k8_space(), SobolScramble::DigitalShift, kMax, std::nullopt);
    require(sobol_identity_input(shifted.descriptor) == read_file("identity-input-k8-shift-max.txt"),
            "identity input (digital_shift, seed 2^64-1)");
    require(shifted.descriptor.identity == read_file("identity-input-k8-shift-max.sha256"),
            "identity (digital_shift, seed 2^64-1)");
    require(*shifted.descriptor.seed == kMax, "the seed keeps all 64 bits");
}

void test_descriptor_rules_and_distinctness() {
    // None records no seed, so seeds cannot split one stream into two identities (N4).
    const Env none_a(discrete_space(), SobolScramble::None, 1, std::nullopt);
    const Env none_b(discrete_space(), SobolScramble::None, 987654321, std::nullopt);
    require(!none_a.descriptor.seed.has_value(), "none must record a null seed");
    require(none_a.descriptor.identity == none_b.descriptor.identity, "none ignores the seed in the identity");

    // Constants take no column; columns are the varying names in byte order.
    require((none_a.descriptor.columns == std::vector<std::string>{"alpha", "flag", "kind"}), "ordered columns");

    // Numeric identity is required exactly when a column uses binary64 math (N11).
    {
        bool threw = false;
        try {
            Env refused(mixed_space(), SobolScramble::DigitalShift, 3, std::nullopt);
        } catch (const TypedHpoError<std::logic_error>& error) {
            threw = error.code() == "hpo_invariant";
        }
        require(threw, "a floating column without a numeric identity must be an invariant breach");
        threw = false;
        try {
            Env refused(discrete_space(), SobolScramble::DigitalShift, 3, std::string(kNumeric));
        } catch (const TypedHpoError<std::logic_error>& error) {
            threw = error.code() == "hpo_invariant";
        }
        require(threw, "a numeric identity for a discrete-only space must be an invariant breach");
        threw = false;
        try {
            Env refused(mixed_space(), SobolScramble::DigitalShift, 3, std::string());
        } catch (const TypedHpoError<std::logic_error>&) {
            threw = true;
        }
        require(threw, "an empty numeric identity must be refused");
    }

    // Every input moves the identity.
    std::set<std::string> identities;
    const auto add = [&](const Env& env) { identities.insert(env.descriptor.identity); };
    add(Env(mixed_space(), SobolScramble::DigitalShift, 1, std::string(kNumeric)));
    add(Env(mixed_space(), SobolScramble::DigitalShift, 2, std::string(kNumeric)));                 // seed
    add(Env(mixed_space(), SobolScramble::None, 1, std::string(kNumeric)));                         // scramble
    add(Env(mixed_space(), SobolScramble::DigitalShift, 1, std::string("portable-sobol-v1;other")));  // numeric
    add(Env(SearchSpace({IntegerDimension("alpha", -50, 51), BooleanDimension("flag"),
                         RealDimension("risk", -1.5, 2.5), RealDimension("grid", 0.0, 1.0, 0.125),
                         RealDimension("rate", 1e-3, 100.0, std::nullopt, true),
                         IntegerDimension("period", 2, 5000, 1, true)}),
            SobolScramble::DigitalShift, 1, std::string(kNumeric)));                                // space
    add(Env(SearchSpace({IntegerDimension("alpha", -50, 50), BooleanDimension("flag2"),
                         RealDimension("risk", -1.5, 2.5), RealDimension("grid", 0.0, 1.0, 0.125),
                         RealDimension("rate", 1e-3, 100.0, std::nullopt, true),
                         IntegerDimension("period", 2, 5000, 1, true)}),
            SobolScramble::DigitalShift, 1, std::string(kNumeric)));                                // columns
    require(identities.size() == 6, "identity inputs must not collide");

    // Mapper contract and revision are bound (AR amendment): changing either moves the identity.
    auto altered = none_a.descriptor;
    altered.mapper_revision += 1;
    require(sobol_identity(altered) != none_a.descriptor.identity, "mapper revision must move the identity");
    altered = none_a.descriptor;
    altered.mapper_contract += "x";
    require(sobol_identity(altered) != none_a.descriptor.identity, "mapper contract must move the identity");
    require(none_a.descriptor.mapper_revision == kSobolMapperRevision && kSobolMapperRevision == 2,
            "the descriptor binds mapper revision 2");

    // The block: exact key set, string 64-bit values, null seed for none, round trip through the parser.
    const Env shifted(discrete_space(), SobolScramble::DigitalShift, kMax, std::nullopt);
    const SobolPartSummary part{18446744073709551ULL, 18446744073709600ULL, 3, false};
    const Json block = parse_json(render_sobol_block(shifted.descriptor, part));
    require(block.members.size() == 12, "the sobol block has exactly twelve keys");
    require(block.members.at("seed").kind == Json::Kind::String && block.members.at("seed").value == "18446744073709551615",
            "seed travels as a decimal string");
    require(block.members.at("first_index").value == "18446744073709551" &&
                block.members.at("next_index").value == "18446744073709600",
            "indices travel as decimal strings");
    require(block.members.at("exact_stream").kind == Json::Kind::Bool && block.members.at("exact_stream").value == "false",
            "exact_stream is a Boolean");
    require(block.members.at("numeric_build_identity").kind == Json::Kind::Null, "discrete-only: null identity");
    require(parse_json(render_sobol_block(none_a.descriptor, part)).members.at("seed").kind == Json::Kind::Null,
            "none: null seed");
    require(block.members.at("mapper").members.at("revision").value == "2", "mapper revision in the block");
}

// ---- part accounting -----------------------------------------------------------------------------

SobolPartSummary account(const std::vector<std::uint64_t>& ids, std::uint64_t first, bool parent_exact) {
    SobolPartAccumulator accumulator;
    for (const auto id : ids)
        accumulator.observe(id);
    return accumulator.summary(first, parent_exact);
}

void test_part_accounting() {
    auto summary = account(range(0, 10), 0, true);
    require(summary.first_index == 0 && summary.next_index == 10 && summary.terminal_trials == 10 && summary.exact_stream,
            "fresh contiguous part");
    summary = account({0, 1, 2, 5, 6}, 0, true);
    require(summary.next_index == 7 && !summary.exact_stream, "a gap in the part ends exactness; holes stay");
    summary = account({5, 6, 7}, 5, true);
    require(summary.next_index == 8 && summary.exact_stream, "continuation of an exact parent");
    summary = account({5, 6, 7}, 5, false);
    require(summary.next_index == 8 && !summary.exact_stream, "an inexact parent is never made exact by the part");
    summary = account({6, 7}, 5, true);
    require(summary.next_index == 8 && !summary.exact_stream, "a part that skips its first index is inexact");
    summary = account({7, 5, 6, 9, 8}, 5, true);
    require(summary.next_index == 10 && summary.exact_stream, "observation order does not matter");

    // Zero terminal trials: next_index == first_index, exactness is the parent's (vacuous part).
    summary = account({}, 0, true);
    require(summary.next_index == 0 && summary.terminal_trials == 0 && summary.exact_stream, "empty fresh part");
    summary = account({}, 12, true);
    require(summary.next_index == 12 && summary.exact_stream, "empty part after an exact parent");
    summary = account({}, 12, false);
    require(summary.next_index == 12 && !summary.exact_stream, "empty part after an inexact parent");

    // High indices and the last issuable ID.
    summary = account({(1ULL << 53) + 1, (1ULL << 53) + 2}, (1ULL << 53) + 1, true);
    require(summary.next_index == (1ULL << 53) + 3 && summary.exact_stream, "IDs beyond 2^53 stay exact");
    summary = account({1ULL << 63, (1ULL << 63) + 1}, 1ULL << 63, true);
    require(summary.next_index == (1ULL << 63) + 2 && summary.exact_stream, "IDs at 2^63");
    summary = account({kMax - 2, kMax - 1}, kMax - 2, true);
    require(summary.next_index == kMax && summary.exact_stream, "the last issuable IDs 2^64-3 and 2^64-2");

    // Integrator bugs are invariant breaches, not silent metadata.
    const auto breaches = [](const std::function<void()>& action) {
        try {
            action();
        } catch (const TypedHpoError<std::logic_error>& error) {
            return error.code() == "hpo_invariant";
        }
        return false;
    };
    require(breaches([] { SobolPartAccumulator a; a.observe(kMax); }), "ID 2^64-1 is reserved");
    require(breaches([] { account({3}, 5, true); }), "an ID below first_index");
    require(breaches([] { account({6, 6}, 5, true); }), "a repeated terminal ID");

    // best-k / none: the summary depends only on the terminal set, never on what is retained.
    SobolPartAccumulator all_terminal;
    std::vector<std::uint64_t> retained;
    for (std::uint64_t id = 0; id < 100; ++id) {
        all_terminal.observe(id);
        if (id % 17 == 0)
            retained.push_back(id);  // the "best-k" rows
    }
    const auto from_terminal = all_terminal.summary(0, true);
    require(from_terminal.next_index == 100 && from_terminal.exact_stream && from_terminal.terminal_trials == 100,
            "terminal accounting sees every trial");
    const auto from_retained = account(retained, 0, true);
    require(from_retained.next_index != 100 && !from_retained.exact_stream,
            "accounting from retained rows alone would be wrong, which is why the accumulator exists");
}

// ---- admission: accepted parents -----------------------------------------------------------------

void test_static_fixture_parent() {
    const Env env(k8_space(), SobolScramble::None, 0, std::nullopt);
    const auto admission = admit_sobol_parent(g_scratch->write(read_file("parent-k8-none-fresh.json")), env.space,
                                              env.recorded, env.descriptor, 4);
    require(admission.next_id == 8 && admission.parent_exact && admission.ancestor_rows == 0 &&
                admission.part_rows == 8 && admission.history.size() == 8,
            "the hand-written complete parent must be admitted as an exact prefix of 8");
    // The hand-written rows are the published van der Corput prefix on an eight-point lattice.
    const std::int64_t lattice[8] = {0, 4, 6, 2, 3, 7, 5, 1};
    for (std::uint64_t id = 0; id < 8; ++id)
        require(std::get<std::int64_t>(admission.history.observations[id].candidate.values.at("k")) == lattice[id],
                "fixture row " + std::to_string(id));

    const auto forged = g_scratch->write(read_file("parent-k8-none-forged-row.json"));
    require_refused([&] { (void)admit_sobol_parent(forged, env.space, env.recorded, env.descriptor, 4); },
                    refusal::kRowForged, "hand-forged row (id 3 carries 3 instead of 2)");
}

void test_chain_of_complete_parts() {
    const Env env(mixed_space(), SobolScramble::DigitalShift, 0x9E3779B97F4A7C15ULL, std::string(kNumeric));
    const auto first = admit(env, make_document(env, {}, range(0, 10)));
    require(first.next_id == 10 && first.parent_exact && first.part_rows == 10 && first.ancestor_rows == 0,
            "part one");
    const auto second = admit(env, make_child_document(env, first, range(10, 8)));
    require(second.next_id == 18 && second.parent_exact && second.ancestor_rows == 10 && second.part_rows == 8,
            "part two continues part one exactly");
    const auto third = admit(env, make_child_document(env, second, range(18, 5)));
    require(third.next_id == 23 && third.parent_exact && third.ancestor_rows == 18 && third.part_rows == 5,
            "part three continues the chain");
    // Shifted PARAMETER stream: history row i is exactly the uninterrupted stream's row i.
    for (std::uint64_t id = 0; id < 23; ++id) {
        const auto& candidate = third.history.observations[id].candidate;
        require(candidate.id == id, "history IDs are 0..22");
        const Candidate expected = env.sampler.at(id);
        for (const auto& [name, value] : expected.values)
            require(sobol_detail::same_parameter(value, candidate.values.at(name)), "row " + std::to_string(id));
    }
    // Duplicate parameter vectors stay distinct occurrences: a Boolean-only space repeats vectors.
    const Env flags(SearchSpace({BooleanDimension("only")}), SobolScramble::DigitalShift, 5, std::nullopt);
    const auto repeated = admit(flags, make_document(flags, {}, range(0, 32)));
    require(repeated.next_id == 32 && repeated.parent_exact && repeated.history.size() == 32,
            "32 points over two vectors are all distinct occurrences");
}

void test_gapped_and_high_id_parents() {
    const Env env(discrete_space(), SobolScramble::DigitalShift, 11, std::nullopt);
    // Lower holes stay holes: the child starts after the largest ID and the parent is inexact.
    const auto gapped = admit(env, make_document(env, {}, {0, 1, 2, 5, 6}));
    require(gapped.next_id == 7 && !gapped.parent_exact && gapped.part_rows == 5, "gapped parent");
    const auto child = admit(env, make_child_document(env, gapped, {7, 8, 9}));
    require(child.next_id == 10 && !child.parent_exact, "a part after an inexact parent is never exact");
    require(child.history.observations[5].candidate.id == 7, "ID 3 and 4 stay holes; the next row is 7");
    // A gapped ANCESTOR set with a contiguous part: still inexact overall.
    const auto after_gap = admit(env, make_document(env, {0, 1, 3}, {4, 5}));
    require(after_gap.next_id == 6 && !after_gap.parent_exact, "ancestors with a hole make the history inexact");

    // High IDs behind a single ancestor row.
    const auto high = admit(env, make_document(env, {(1ULL << 63) - 1}, {1ULL << 63, (1ULL << 63) + 1, (1ULL << 63) + 2}));
    require(high.next_id == (1ULL << 63) + 3 && !high.parent_exact, "IDs around 2^63");
    const auto beyond = admit(env, make_document(env, {(1ULL << 53)}, {(1ULL << 53) + 1}));
    require(beyond.next_id == (1ULL << 53) + 2 && !beyond.parent_exact, "IDs around 2^53");

    // The last issuable IDs: largest ID 2^64-3 leaves exactly ID 2^64-2.
    const auto doc = make_document(env, {kMax - 3}, {kMax - 2});
    require(admit(env, doc, 1).next_id == kMax - 1, "budget 1 fits the last issuable ID 2^64-2");
    expect_refusal(env, doc, refusal::kBudget, "budget 2 exceeds the remaining IDs", {}, 2);
    // Largest ID 2^64-2 leaves nothing.
    expect_refusal(env, make_document(env, {kMax - 3}, {kMax - 1}), refusal::kBudget, "no ID remains", {}, 0);
    expect_refusal(env, make_document(env, {kMax - 3}, {kMax - 1}), refusal::kBudget, "no ID remains, budget 1", {}, 1);
}

void test_zero_terminal_part_and_empty_history() {
    const Env env(discrete_space(), SobolScramble::DigitalShift, 11, std::nullopt);
    const auto empty_part = admit(env, make_document(env, range(0, 6), {}));
    require(empty_part.next_id == 6 && empty_part.parent_exact && empty_part.part_rows == 0 &&
                empty_part.ancestor_rows == 6,
            "a part with no terminal trial leaves next_index == first_index");
    const auto holed = admit(env, make_document(env, {0, 1, 4}, {}));
    require(holed.next_id == 5 && !holed.parent_exact, "an empty part after gapped ancestors is inexact");
    // Nothing at all: refused explicitly, like the generic loader's "parent has no trials".
    expect_refusal(env, make_document(env, {}, {}), refusal::kNoRows, "no rows anywhere");
}

void test_unsorted_arrays_are_normalized() {
    const Env env(discrete_space(), SobolScramble::DigitalShift, 11, std::nullopt);
    auto document = make_document(env, range(0, 5), range(5, 6));
    std::mt19937_64 engine(7);
    const auto shuffle = [&](const char* key) {
        auto& items = document.members.at(key).items;
        std::shuffle(items.begin(), items.end(), engine);
    };
    shuffle("trials");
    shuffle("warm_start_trials");
    const auto admission = admit(env, document);
    require(admission.next_id == 11 && admission.parent_exact && admission.history.size() == 11,
            "row order carries no meaning: point-by-ID");
    require(admission.history.observations.front().candidate.id == 0 && admission.history.observations.back().candidate.id == 10,
            "the generic loader returns the history sorted by ID");
}

// ---- admission: refused formats ------------------------------------------------------------------

void test_foreign_and_lossy_formats_are_refused() {
    const Env env(discrete_space(), SobolScramble::DigitalShift, 11, std::nullopt);
    const Json honest = make_document(env, {}, range(0, 4));
    const auto refuse_text = [&](const std::string& text, const char* expected, const char* what) {
        const auto path = g_scratch->write(text);
        require_refused([&] { (void)admit_sobol_parent(path, env.space, env.recorded, env.descriptor, 1); }, expected, what);
    };
    // Rows-only array and JSONL of otherwise valid rows: provenance is lost.
    Json rows = array_json();
    for (const auto& row : honest.members.at("trials").items)
        rows.items.push_back(row);
    refuse_text(dump_json(rows), refusal::kNotResult, "rows-only array");
    std::string jsonl;
    for (const auto& row : honest.members.at("trials").items)
        jsonl += dump_json(row) + "\n";
    refuse_text(jsonl, refusal::kNotResult, "JSONL");
    refuse_text(dump_json(honest.members.at("trials").items.front()), refusal::kNotResult, "a single row object");
    refuse_text(std::string("PFHWARM\0garbage", 15), refusal::kBinary, "binary v2 magic");
    refuse_text("", refusal::kNotResult, "empty file");
    refuse_text("{\"trials\":", refusal::kNotResult, "truncated JSON");
    refuse_text(dump_json(edited(honest, [](Json& d) { d.members.erase("trials"); })), refusal::kNotResult, "no trials key");
    refuse_text(dump_json(edited(honest, [](Json& d) { d.members["trials"] = Json::string("x"); })), refusal::kNotResult,
                "trials not an array");

    // Summary / best-k / none results retain too little history.
    for (const char* mode : {"best-k", "none"})
        expect_refusal(env, edited(honest, [&](Json& d) { d.members["trials_out"] = Json::string(mode); }),
                       refusal::kNotFullHistory, std::string("trials_out ") + mode);
    expect_refusal(env, edited(honest, [](Json& d) { d.members.erase("trials_out"); }), refusal::kNotFullHistory,
                   "trials_out missing");
    expect_refusal(env, edited(honest, [](Json& d) { d.members["trials_out"] = Json::boolean(true); }),
                   refusal::kNotFullHistory, "trials_out not a string");

    // Foreign samplers, even with perfectly valid rows and a sobol block copied in.
    for (const char* sampler : {"random", "grid", "tpe", "dlib_global", "Sobol", ""})
        expect_refusal(env, edited(honest, [&](Json& d) { d.members["sampler"] = Json::string(sampler); }),
                       refusal::kForeignSampler, std::string("sampler ") + sampler);
    expect_refusal(env, edited(honest, [](Json& d) { d.members.erase("sampler"); }), refusal::kForeignSampler,
                   "sampler missing");
    expect_refusal(env, edited(honest, [](Json& d) { d.members.erase("sobol"); }), refusal::kBlockInvalid,
                   "sobol block missing");
    expect_refusal(env, edited(honest, [](Json& d) { d.members["sobol"] = Json::string("x"); }), refusal::kBlockInvalid,
                   "sobol block not an object");
    // An unreadable path is a refusal that does not name the path.
    require_refused([&] {
        (void)admit_sobol_parent(g_scratch->dir / "missing.json", env.space, env.recorded, env.descriptor, 1);
    }, refusal::kUnreadable, "missing file");
}

// ---- admission: forged descriptors ---------------------------------------------------------------

void test_descriptor_forgery_is_refused() {
    const Env env(mixed_space(), SobolScramble::DigitalShift, 0x1234ULL, std::string(kNumeric));
    const Json honest = make_document(env, {}, range(0, 4));
    require(admit(env, honest).next_id == 4, "the honest document is admitted");
    const auto block = [](Json& d) -> Json& { return d.members.at("sobol"); };
    const auto forged = [&](const std::string& what, const std::string& expected, const std::function<void(Json&)>& edit,
                            const std::vector<std::string>& leak = {}) {
        expect_refusal(env, edited(honest, edit), expected, what, leak);
    };

    forged("contract", field_differs("contract"), [&](Json& d) { block(d).members["contract"] = Json::string("pineforge_sobol_v2"); });
    forged("table name", field_differs("table"), [&](Json& d) { block(d).members["table"].members["name"] = Json::string("other"); });
    forged("table subset", field_differs("table"),
           [&](Json& d) { block(d).members["table"].members["subset_sha256"] = Json::string(std::string(64, 'a')); });
    forged("table upstream", field_differs("table"),
           [&](Json& d) { block(d).members["table"].members["upstream_sha256"] = Json::string(std::string(64, 'b')); });
    forged("word bits", field_differs("word_bits"), [&](Json& d) { block(d).members["word_bits"] = Json::number("32"); });
    forged("scramble", field_differs("scramble"), [&](Json& d) { block(d).members["scramble"] = Json::string("none"); });
    forged("scramble spelling", refusal::kBlockInvalid, [&](Json& d) { block(d).members["scramble"] = Json::string("owen"); });
    forged("seed value", field_differs("seed"), [&](Json& d) { block(d).members["seed"] = Json::string("4661"); });
    forged("seed null", field_differs("seed"), [&](Json& d) { block(d).members["seed"] = Json{}; });
    forged("seed as number", refusal::kBlockInvalid, [&](Json& d) { block(d).members["seed"] = Json::number("4660"); });
    for (const char* text : {"04660", "+4660", " 4660", "4660 ", "18446744073709551616", "0x1234"})
        forged(std::string("seed text ") + text, refusal::kBlockInvalid,
               [&](Json& d) { block(d).members["seed"] = Json::string(text); }, {text});
    forged("top-level seed", field_differs("seed"), [](Json& d) { d.members["seed"] = Json::number("4661"); });
    forged("top-level seed missing", field_differs("seed"), [](Json& d) { d.members.erase("seed"); });
    forged("columns reordered", field_differs("columns"), [&](Json& d) {
        std::swap(block(d).members["columns"].items.front(), block(d).members["columns"].items.back());
    });
    forged("columns extra", field_differs("columns"),
           [&](Json& d) { block(d).members["columns"].items.push_back(Json::string("forged_column")); }, {"forged_column"});
    forged("columns missing", field_differs("columns"), [&](Json& d) { block(d).members["columns"].items.pop_back(); });
    forged("column not a string", refusal::kBlockInvalid,
           [&](Json& d) { block(d).members["columns"].items.front() = Json::number("1"); });
    forged("numeric identity", field_differs("numeric_build_identity"),
           [&](Json& d) { block(d).members["numeric_build_identity"] = Json::string("portable-sobol-v1;forged"); },
           {"portable-sobol-v1;forged"});
    forged("numeric identity dropped", field_differs("numeric_build_identity"),
           [&](Json& d) { block(d).members["numeric_build_identity"] = Json{}; });
    forged("flags marker in the parent only", field_differs("numeric_build_identity"), [&](Json& d) {
        block(d).members["numeric_build_identity"] = Json::string(std::string(kNumeric) + ";flags_sha256:unavailable");
    });
    forged("mapper revision", field_differs("mapper"), [&](Json& d) { block(d).members["mapper"].members["revision"] = Json::number("1"); });
    forged("mapper contract", field_differs("mapper"),
           [&](Json& d) { block(d).members["mapper"].members["contract"] = Json::string("pineforge_sobol_mapper_v0"); });
    forged("extra block key", refusal::kBlockInvalid, [&](Json& d) { block(d).members["extra"] = Json::boolean(true); });
    forged("missing block key", refusal::kBlockInvalid, [&](Json& d) { block(d).members.erase("exact_stream"); });
    forged("exact_stream not Boolean", refusal::kBlockInvalid, [&](Json& d) { block(d).members["exact_stream"] = Json::string("true"); });
    forged("implementation", field_differs("implementation"),
           [](Json& d) { d.members["sampler_implementation"] = Json::string("pineforge_sobol_gray32_v0"); });
    forged("space hash", field_differs("space_hash"), [](Json& d) { d.members["space_hash"] = Json::string(std::string(64, '0')); });
    forged("space hash version missing", field_differs("space_hash"), [](Json& d) { d.members.erase("space_hash_version"); });

    // The identity string itself: a flipped digit is a forged hash, whatever the other fields say.
    const std::string identity = env.descriptor.identity;
    std::string flipped = identity;
    flipped.back() = flipped.back() == '0' ? '1' : '0';
    forged("identity flipped", refusal::kIdentityForged, [&](Json& d) { block(d).members["identity"] = Json::string(flipped); },
           {flipped});
    forged("identity upper case", refusal::kBlockInvalid, [&](Json& d) {
        std::string upper = identity;
        std::transform(upper.begin(), upper.end(), upper.begin(), [](unsigned char c) { return std::toupper(c); });
        if (upper == identity)
            upper[0] = 'G';
        block(d).members["identity"] = Json::string(upper);
    });
    forged("identity short", refusal::kBlockInvalid, [&](Json& d) { block(d).members["identity"] = Json::string(identity.substr(1)); });

    // Parent from another run (other seed), self-consistent in every field including its identity hash.
    const Env other(mixed_space(), SobolScramble::DigitalShift, 0x1235ULL, std::string(kNumeric));
    expect_refusal(env, make_document(other, {}, range(0, 4)), field_differs("seed"), "a self-consistent foreign seed");
    const Env other_numeric(mixed_space(), SobolScramble::DigitalShift, 0x1234ULL, std::string("portable-sobol-v1;other"));
    expect_refusal(env, make_document(other_numeric, {}, range(0, 4)), field_differs("numeric_build_identity"),
                   "a self-consistent foreign build");
    // The flags marker on the run's own side: the identity cannot be continued (N11).
    const Env unbound(mixed_space(), SobolScramble::DigitalShift, 0x1234ULL, std::string(kNumeric) + ";flags_sha256:unavailable");
    expect_refusal(unbound, make_document(unbound, {}, range(0, 4)), refusal::kNumericUnavailable,
                   "flags_sha256:unavailable");
}

// ---- admission: indices, counts, duplicates ------------------------------------------------------

void test_index_and_count_contradictions_are_refused() {
    const Env env(discrete_space(), SobolScramble::DigitalShift, 21, std::nullopt);
    const Json chained = make_document(env, range(0, 5), range(5, 4));
    require(admit(env, chained).next_id == 9, "the honest two-part document is admitted");
    const auto block = [](Json& d) -> Json& { return d.members.at("sobol"); };

    expect_refusal(env, edited(chained, [&](Json& d) { block(d).members["first_index"] = Json::string("0"); }), refusal::kIndices,
                   "first_index 0 with ancestors");
    expect_refusal(env, edited(chained, [&](Json& d) { block(d).members["first_index"] = Json::string("6"); }), refusal::kIndices,
                   "first_index beyond the ancestors");
    expect_refusal(env, edited(chained, [&](Json& d) { block(d).members["next_index"] = Json::string("8"); }), refusal::kIndices,
                   "next_index one short");
    expect_refusal(env, edited(chained, [&](Json& d) { block(d).members["next_index"] = Json::string("10"); }), refusal::kIndices,
                   "next_index one beyond");
    expect_refusal(env, edited(chained, [&](Json& d) { block(d).members["next_index"] = Json::string("18446744073709551615"); }),
                   refusal::kIndices, "next_index 2^64-1");
    const Json fresh = make_document(env, {}, range(0, 4));
    expect_refusal(env, edited(fresh, [&](Json& d) { block(d).members["first_index"] = Json::string("3"); }), refusal::kIndices,
                   "first_index 3 without ancestors");
    // A part row below first_index (ancestors 0 and 2 end at 2, so first_index is 3; the part holds 1).
    expect_refusal(env, edited(make_document(env, {0, 2}, {5}), [](Json& d) {
                       d.members.at("trials").items.front().members["trial_id"] = Json::number("1");
                   }), refusal::kIndices, "a part ID below first_index");

    // exact_stream is recomputed from the rows; a lie in either direction is refused.
    expect_refusal(env, edited(chained, [&](Json& d) { block(d).members["exact_stream"] = Json::boolean(false); }),
                   refusal::kExactClaim, "exact claimed false on exact rows");
    const Json gapped = make_document(env, {}, {0, 1, 2, 5, 6});
    require(admit(env, gapped).next_id == 7, "the honest gapped document is admitted");
    expect_refusal(env, edited(gapped, [&](Json& d) { block(d).members["exact_stream"] = Json::boolean(true); }),
                   refusal::kExactClaim, "exact claimed true on a gapped part");
    expect_refusal(env, edited(make_document(env, {0, 1, 3}, {4, 5}),
                               [&](Json& d) { block(d).members["exact_stream"] = Json::boolean(true); }),
                   refusal::kExactClaim, "exact claimed true after gapped ancestors");

    // Counts.
    expect_refusal(env, edited(chained, [](Json& d) { d.members["trials_completed"] = Json::number("5"); }), refusal::kCounts,
                   "trials_completed too large");
    expect_refusal(env, edited(chained, [](Json& d) { d.members["trials_completed"] = Json::number("3"); }), refusal::kCounts,
                   "trials_completed too small");
    expect_refusal(env, edited(chained, [](Json& d) { d.members.erase("trials_completed"); }), "trials_completed",
                   "trials_completed missing");
    expect_refusal(env, edited(chained, [](Json& d) { d.members.at("warm_start").members["trials"] = Json::number("4"); }),
                   refusal::kCounts, "warm_start.trials too small");
    expect_refusal(env, edited(chained, [](Json& d) { d.members.erase("warm_start"); }), refusal::kCounts,
                   "ancestors without warm_start");
    expect_refusal(env, edited(chained, [](Json& d) { d.members.erase("warm_start_trials"); }), refusal::kCounts,
                   "warm_start without its ancestors");
    expect_refusal(env, edited(chained, [](Json& d) { d.members["warm_start_trials"] = Json::string("x"); }), refusal::kCounts,
                   "ancestors not an array");

    // Duplicates: inside the part, inside the ancestors, and across them.
    expect_refusal(env, edited(fresh, [](Json& d) {
                       d.members.at("trials").items.push_back(d.members.at("trials").items.front());
                       d.members["trials_completed"] = Json::number("5");
                   }), refusal::kDuplicateId, "duplicate in the part");
    expect_refusal(env, edited(chained, [](Json& d) {
                       d.members.at("trials").items.front() = d.members.at("warm_start_trials").items.front();
                   }), refusal::kDuplicateId, "ID shared by an ancestor and the part");
    expect_refusal(env, edited(chained, [](Json& d) {
                       d.members.at("warm_start_trials").items.back() = d.members.at("warm_start_trials").items.front();
                   }), refusal::kDuplicateId, "duplicate among the ancestors");

    // Trial-ID terminal restrictions: 2^64-1 is refused by the generic ID parser, 2^64 is not an ID.
    expect_refusal(env, edited(fresh, [](Json& d) {
                       d.members.at("trials").items.back().members["trial_id"] = Json::number("18446744073709551615");
                   }), "continuation ID", "a row with ID 2^64-1");
    expect_refusal(env, edited(fresh, [](Json& d) {
                       d.members.at("trials").items.back().members["trial_id"] = Json::number("18446744073709551616");
                   }), "", "a row with ID 2^64");
    expect_refusal(env, edited(fresh, [](Json& d) {
                       d.members.at("trials").items.back().members["trial_id"] = Json::number("-1");
                   }), "", "a row with a negative ID");
    expect_refusal(env, edited(fresh, [](Json& d) {
                       d.members.at("trials").items.back().members["trial_id"] = Json::number("3.0");
                   }), "", "a row with a fractional ID");
}

// ---- admission: forged rows ----------------------------------------------------------------------

void test_forged_rows_are_refused() {
    const Env env(mixed_space(), SobolScramble::DigitalShift, 31, std::string(kNumeric));
    const Json chained = make_document(env, range(0, 6), range(6, 5));
    require(admit(env, chained).next_id == 11, "the honest document is admitted");
    const auto parameter = [](Json& d, const char* array, std::size_t row, const char* name) -> Json& {
        return d.members.at(array).items.at(row).members.at("parameters").members.at(name);
    };

    for (const char* array : {"trials", "warm_start_trials"}) {
        const std::string where = std::string(" in ") + array;
        const auto forge = [&](const std::string& what, const std::function<void(Json&)>& edit, const std::string& expected) {
            expect_refusal(env, edited(chained, edit), expected, what + where);
        };
        const Candidate point = env.sampler.at(array[0] == 't' ? 7 : 2);
        forge("integer off by one", [&](Json& d) {
            parameter(d, array, array[0] == 't' ? 1 : 2, "alpha") = scalar_json(ParameterValue(std::get<std::int64_t>(point.values.at("alpha")) + 1));
        }, refusal::kRowForged);
        forge("Boolean flipped", [&](Json& d) {
            parameter(d, array, array[0] == 't' ? 1 : 2, "flag") = Json::boolean(!std::get<bool>(point.values.at("flag")));
        }, refusal::kRowForged);
        forge("real one ulp up", [&](Json& d) {
            const double value = std::get<double>(point.values.at("risk"));
            parameter(d, array, array[0] == 't' ? 1 : 2, "risk") =
                scalar_json(ParameterValue(std::nextafter(value, std::numeric_limits<double>::infinity())));
        }, refusal::kRowForged);
        forge("log real one ulp down", [&](Json& d) {
            const double value = std::get<double>(point.values.at("rate"));
            parameter(d, array, array[0] == 't' ? 1 : 2, "rate") =
                scalar_json(ParameterValue(std::nextafter(value, 0.0)));
        }, refusal::kRowForged);
        forge("another valid grid value", [&](Json& d) {
            const double value = std::get<double>(point.values.at("grid"));
            parameter(d, array, array[0] == 't' ? 1 : 2, "grid") = scalar_json(ParameterValue(value == 0.5 ? 0.625 : 0.5));
        }, refusal::kRowForged);
        forge("log integer shifted", [&](Json& d) {
            const auto value = std::get<std::int64_t>(point.values.at("period"));
            parameter(d, array, array[0] == 't' ? 1 : 2, "period") = scalar_json(ParameterValue(value == 2 ? std::int64_t{3} : value - 1));
        }, refusal::kRowForged);
        // Valid point of ANOTHER ID: point-by-ID meaning is enforced.
        forge("rows with swapped IDs", [&](Json& d) {
            auto& items = d.members.at(array).items;
            std::swap(items.at(0).members["trial_id"], items.at(1).members["trial_id"]);
        }, refusal::kRowForged);
    }

    // Shape problems are the generic loader's refusals (still typed exit 4).
    expect_refusal(env, edited(chained, [&](Json& d) { d.members.at("trials").items[0].members.at("parameters").members["extra"] = Json::number("1"); }),
                   "", "an extra parameter");
    expect_refusal(env, edited(chained, [&](Json& d) { d.members.at("trials").items[0].members.at("parameters").members.erase("risk"); }),
                   "", "a missing parameter");
    expect_refusal(env, edited(chained, [&](Json& d) { parameter(d, "trials", 0, "risk") = Json::number("999.0"); }), "",
                   "a parameter outside its range");
    expect_refusal(env, edited(chained, [&](Json& d) { parameter(d, "trials", 0, "alpha") = Json::number("9223372036854775808"); }),
                   "", "an unrepresentable integer");
    expect_refusal(env, edited(chained, [&](Json& d) { parameter(d, "trials", 0, "alpha") = Json::string("3"); }), "",
                   "a mistyped parameter");
    expect_refusal(env, edited(chained, [&](Json& d) { d.members.at("trials").items[0].members["status"] = Json::string("fake"); }),
                   "", "an unknown status");
    expect_refusal(env, edited(chained, [&](Json& d) { d.members["space"].members.at("parameters").members.at("alpha").members["high"] = Json::number("51"); }),
                   "", "a different recorded space");
}

// ---- admission: budget ---------------------------------------------------------------------------

void test_budget_is_checked_before_any_trial() {
    const Env env(discrete_space(), SobolScramble::DigitalShift, 5, std::nullopt);
    const Json parent = make_document(env, {}, range(0, 4));
    require(admit(env, parent, 0).next_id == 4, "budget 0 (wall-limited) is accepted");
    require(admit(env, parent, kMax - 4).next_id == 4, "the largest budget that fits");
    expect_refusal(env, parent, refusal::kBudget, "one ID too many", {}, kMax - 3);
    expect_refusal(env, parent, refusal::kBudget, "budget 2^64-1", {}, kMax);
}

// ---- diagnostics ---------------------------------------------------------------------------------

void test_diagnostics_are_source_stable() {
    // Every sobol_refusal text is fixed; field-specific ones differ only by a static field name.
    for (const char* text : {refusal::kNotResult, refusal::kBinary, refusal::kUnreadable, refusal::kForeignSampler,
                             refusal::kNotFullHistory, refusal::kBlockInvalid, refusal::kNumericUnavailable,
                             refusal::kIdentityForged, refusal::kIdentityMismatch, refusal::kNoRows, refusal::kDuplicateId,
                             refusal::kCounts, refusal::kIndices, refusal::kExactClaim, refusal::kChanged, refusal::kRowForged,
                             refusal::kBudget}) {
        const std::string message = text;
        require(!message.empty() && message.find('{') == std::string::npos,
                "refusal texts must be plain sentences: " + message);
    }
    std::set<std::string> distinct;
    for (const char* text : {refusal::kNotResult, refusal::kBinary, refusal::kUnreadable, refusal::kForeignSampler,
                             refusal::kNotFullHistory, refusal::kBlockInvalid, refusal::kNumericUnavailable,
                             refusal::kIdentityForged, refusal::kIdentityMismatch, refusal::kNoRows, refusal::kDuplicateId,
                             refusal::kCounts, refusal::kIndices, refusal::kExactClaim, refusal::kChanged, refusal::kRowForged,
                             refusal::kBudget})
        distinct.insert(text);
    require(distinct.size() == 17, "each refusal reason has its own text");
    // The refusal is the existing typed warm-start error.
    const WarmStartError error("x");
    require(error.code() == "hpo_warm_start_rejected", "WarmStartError code");
    require(std::string(error.what()).rfind("warm-start incompatible: ", 0) == 0, "WarmStartError prefix");
}

}  // namespace

int main() {
    Scratch scratch;
    g_scratch = &scratch;
    struct Test {
        const char* name;
        void (*run)();
    };
    const Test tests[] = {
        {"constants_and_decimal_strings", test_constants_and_decimal_strings},
        {"identity_matches_independent_fixtures", test_identity_matches_independent_fixtures},
        {"descriptor_rules_and_distinctness", test_descriptor_rules_and_distinctness},
        {"part_accounting", test_part_accounting},
        {"static_fixture_parent", test_static_fixture_parent},
        {"chain_of_complete_parts", test_chain_of_complete_parts},
        {"gapped_and_high_id_parents", test_gapped_and_high_id_parents},
        {"zero_terminal_part_and_empty_history", test_zero_terminal_part_and_empty_history},
        {"unsorted_arrays_are_normalized", test_unsorted_arrays_are_normalized},
        {"foreign_and_lossy_formats_are_refused", test_foreign_and_lossy_formats_are_refused},
        {"descriptor_forgery_is_refused", test_descriptor_forgery_is_refused},
        {"index_and_count_contradictions_are_refused", test_index_and_count_contradictions_are_refused},
        {"forged_rows_are_refused", test_forged_rows_are_refused},
        {"budget_is_checked_before_any_trial", test_budget_is_checked_before_any_trial},
        {"diagnostics_are_source_stable", test_diagnostics_are_source_stable},
    };
    int failures = 0;
    for (const Test& test : tests) {
        try {
            test.run();
            std::cout << "PASS " << test.name << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cerr << "FAIL " << test.name << ": " << error.what() << '\n';
        }
    }
    std::cout << (failures == 0 ? "all sobol continuation tests passed" : "sobol continuation tests failed") << '\n';
    return failures == 0 ? 0 : 1;
}
