#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>

#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

template <typename Exception, typename Function>
void require_throws(Function&& function, const std::string& message) {
    try {
        function();
    } catch (const Exception&) {
        return;
    } catch (const std::exception& error) {
        throw std::runtime_error(message + "; unexpected exception: " + error.what());
    } catch (...) {
        throw std::runtime_error(message + "; unexpected non-standard exception");
    }
    throw std::runtime_error(message + "; no exception was thrown");
}

pineforge::hpo::SearchSpace make_two_by_two_space() {
    using namespace pineforge::hpo;
    return SearchSpace({IntegerDimension("x", 0, 1), BooleanDimension("enabled")});
}

pineforge::hpo::TpeSampler make_finite_tpe(const pineforge::hpo::SearchSpace& space,
                                           std::uint64_t seed,
                                           std::uint64_t trials,
                                           pineforge::hpo::CandidatePolicy policy) {
    pineforge::hpo::TpeSamplerConfig config;
    config.startup_trials = 10;
    config.ei_candidates = 8;
    return pineforge::hpo::TpeSampler(space, seed, pineforge::hpo::ObjectiveDirection::Maximize,
                                      trials, config, policy);
}

void test_mixed_radix_rank_round_trip() {
    using namespace pineforge::hpo;

    const SearchSpace space(
        {IntegerDimension("length", 1, 5, 2), RealDimension("risk", 0.5, 1.0, 0.25),
         BooleanDimension("enabled"),
         CategoricalDimension("mode", {std::string("ema"), std::string("sma")})});
    const auto cardinality = space.finite_cardinality();
    require(cardinality.has_value() && *cardinality == 36,
            "mixed finite-space cardinality is incorrect");

    std::set<std::map<std::string, std::string>> serialized;
    for (std::uint64_t ordinal = 0; ordinal < *cardinality; ++ordinal) {
        const Candidate candidate = space.candidate_at(ordinal, ordinal + 100);
        require(candidate.id == ordinal + 100, "candidate_at did not preserve the requested id");
        require(space.is_valid(candidate), "candidate_at produced an invalid candidate");
        require(space.candidate_ordinal(candidate) == ordinal,
                "finite mixed-radix rank/unrank did not round-trip");
        require(serialized.insert(space.serialize_candidate(candidate)).second,
                "two finite ordinals serialized to the same ABI parameter vector");
    }
    require(serialized.size() == *cardinality,
            "finite rank enumeration did not cover the full search space");

    const Candidate first = space.candidate_at(0);
    const Candidate second = space.candidate_at(1);
    const Candidate third = space.candidate_at(2);
    require(std::get<std::string>(*first.find("mode")) == "ema" &&
                std::get<std::string>(*second.find("mode")) == "sma" &&
                !std::get<bool>(*first.find("enabled")) && std::get<bool>(*third.find("enabled")),
            "last-declared-dimension-fastest enumeration order changed");

    require_throws<std::out_of_range>([&]() { (void)space.candidate_at(*cardinality); },
                                      "candidate_at accepted the end ordinal");

    Candidate noncanonical = space.candidate_at(0);
    noncanonical.values["risk"] = std::nextafter(0.5, 1.0);
    require_throws<std::invalid_argument>([&]() { (void)space.candidate_ordinal(noncanonical); },
                                          "finite encoder accepted a noncanonical real value");
}

void test_real_grid_endpoint_semantics() {
    using namespace pineforge::hpo;

    const SearchSpace off_lattice({RealDimension("x", 0.0, 1.0, 0.3)});
    const auto off_cardinality = off_lattice.finite_cardinality();
    require(off_cardinality.has_value() && *off_cardinality == 4,
            "off-lattice high was added as an extra real-grid point");
    const double off_last = std::get<double>(*off_lattice.candidate_at(3).find("x"));
    require(off_last < 1.0 && std::abs(off_last - 0.9) < 1e-15,
            "off-lattice real grid did not end at low + 3 * step");

    const SearchSpace decimal_endpoint({RealDimension("x", 0.0, 0.3, 0.1)});
    const auto decimal_cardinality = decimal_endpoint.finite_cardinality();
    require(decimal_cardinality.has_value() && *decimal_cardinality == 4,
            "0.3 / 0.1 endpoint did not produce four canonical points");
    const Candidate endpoint = decimal_endpoint.candidate_at(3);
    require(std::get<double>(*endpoint.find("x")) == 0.3,
            "one-ULP decimal endpoint was not snapped to the declared high");
    require(decimal_endpoint.candidate_ordinal(endpoint) == 3,
            "snapped decimal endpoint did not round-trip to its ordinal");

    const double maximum = std::numeric_limits<double>::max();
    const SearchSpace maximum_endpoint({RealDimension("x", 0.0, maximum, maximum)});
    const auto maximum_cardinality = maximum_endpoint.finite_cardinality();
    require(maximum_cardinality.has_value() && *maximum_cardinality == 2,
            "overflowed point beyond DBL_MAX was included as a snapped endpoint");
    require(std::get<double>(*maximum_endpoint.candidate_at(1).find("x")) == maximum,
            "DBL_MAX endpoint was not retained as the final canonical point");

    const SearchSpace symmetric_maximum({RealDimension("x", -maximum, maximum, maximum)});
    const auto symmetric_cardinality = symmetric_maximum.finite_cardinality();
    require(symmetric_cardinality.has_value() && *symmetric_cardinality == 3,
            "overflow-safe symmetric DBL_MAX grid did not contain three points");
    const Candidate symmetric_middle = symmetric_maximum.candidate_at(1);
    const Candidate symmetric_last = symmetric_maximum.candidate_at(2);
    require(std::get<double>(*symmetric_middle.find("x")) == 0.0 &&
                std::get<double>(*symmetric_last.find("x")) == maximum,
            "overflow-safe symmetric DBL_MAX grid decoded incorrectly");
    require(symmetric_maximum.is_valid(symmetric_last) &&
                symmetric_maximum.candidate_ordinal(symmetric_last) == 2,
            "overflow-safe symmetric DBL_MAX endpoint did not validate and round-trip");
}

void test_fixed_and_continuous_real_semantics() {
    using namespace pineforge::hpo;

    const SearchSpace fixed({RealDimension("fixed", 0.25, 0.25),
                             RealDimension("fixed_log", 2.0, 2.0, std::nullopt, true)});
    const auto fixed_cardinality = fixed.finite_cardinality();
    require(fixed_cardinality.has_value() && *fixed_cardinality == 1,
            "fixed unstepped real dimensions were not finite");
    const Candidate only = fixed.candidate_at(0);
    require(fixed.candidate_ordinal(only) == 0,
            "fixed unstepped real candidate did not round-trip");
    GridSampler fixed_grid(fixed);
    require(fixed_grid.total_candidates() == 1 && fixed_grid.next().has_value() &&
                !fixed_grid.next().has_value(),
            "GridSampler did not accept a fixed unstepped real dimension");

    const SearchSpace continuous({RealDimension("x", 0.0, 1.0)});
    require(!continuous.finite_cardinality().has_value(),
            "varying unstepped real was reported as finite");
    require_throws<std::invalid_argument>([&]() { (void)continuous.candidate_at(0); },
                                          "continuous real accepted finite indexing");
    require_throws<std::invalid_argument>(
        [&]() { (void)make_finite_tpe(continuous, 7, 10, CandidatePolicy::WithoutReplacement); },
        "without-replacement TPE accepted a continuous real dimension");
}

void test_cardinality_and_abi_rejections() {
    using namespace pineforge::hpo;

    const SearchSpace full_integer({IntegerDimension("x", std::numeric_limits<std::int64_t>::min(),
                                                     std::numeric_limits<std::int64_t>::max())});
    require_throws<std::overflow_error>([&]() { (void)full_integer.finite_cardinality(); },
                                        "2^64 integer grid cardinality did not overflow");

    const SearchSpace product_overflow(
        {IntegerDimension("x", 0, 4'294'967'295LL), IntegerDimension("y", 0, 4'294'967'295LL)});
    require_throws<std::overflow_error>([&]() { (void)product_overflow.finite_cardinality(); },
                                        "2^64 Cartesian product cardinality did not overflow");

    constexpr double kTwoTo53 = 9'007'199'254'740'992.0;
    require_throws<std::invalid_argument>(
        [&]() { (void)RealDimension("too_many", 0.0, kTwoTo53, 1.0); },
        "stepped-real grid accepted more than 2^53 exactly indexable values");
    require_throws<std::invalid_argument>(
        [&]() { (void)RealDimension("collapsed", kTwoTo53 - 2.0, kTwoTo53 + 4.0, 1.0); },
        "stepped-real grid accepted duplicate binary64/ABI values");

    require_throws<std::invalid_argument>(
        []() {
            (void)CategoricalDimension("choice",
                                       {ParameterValue(std::int64_t{1}), ParameterValue(1.0)});
        },
        "categorical int 1 and real 1.0 ABI collision was accepted");
    require_throws<std::invalid_argument>(
        []() {
            (void)CategoricalDimension(
                "choice", {ParameterValue(std::int64_t{1}), ParameterValue(std::string("1"))});
        },
        "categorical int 1 and string 1 ABI collision was accepted");
    require_throws<std::invalid_argument>(
        []() {
            (void)CategoricalDimension("choice",
                                       {ParameterValue(true), ParameterValue(std::string("true"))});
        },
        "categorical bool true and string true ABI collision was accepted");
}

void test_exhaustive_tpe_matches_grid_before_tell() {
    using namespace pineforge::hpo;

    const SearchSpace space = make_two_by_two_space();
    GridSampler grid(space);
    std::set<std::uint64_t> grid_ordinals;
    while (const auto candidate = grid.next()) {
        grid_ordinals.insert(space.candidate_ordinal(*candidate));
    }
    require(grid_ordinals.size() == 4, "2x2 grid did not contain four candidates");

    TpeSampler sampler = make_finite_tpe(space, 17, 4, CandidatePolicy::Exhaustive);
    std::set<std::uint64_t> tpe_ordinals;
    std::vector<std::uint64_t> pending_ids;
    for (std::uint64_t expected_id = 0; expected_id < 4; ++expected_id) {
        const auto candidate = sampler.ask();
        require(candidate.has_value(), "exhaustive TPE ended before covering the 2x2 space");
        require(candidate->id == expected_id, "exhaustive TPE candidate id sequence drifted");
        require(tpe_ordinals.insert(space.candidate_ordinal(*candidate)).second,
                "exhaustive TPE repeated a candidate before any tell");
        pending_ids.push_back(candidate->id);
    }
    require(!sampler.ask().has_value(), "exhaustive TPE exceeded finite cardinality");
    require(tpe_ordinals == grid_ordinals,
            "exhaustive TPE candidate set differed from GridSampler");
    require(sampler.generated() == 4 && sampler.outstanding() == 4,
            "exhaustive pre-tell lifecycle counters are incorrect");
    for (const std::uint64_t id : pending_ids) {
        sampler.abandon(id);
    }
    require(sampler.outstanding() == 0, "exhaustive cleanup left pending candidates");
}

void test_without_replacement_lifecycle_and_limits() {
    using namespace pineforge::hpo;

    const SearchSpace space = make_two_by_two_space();
    TpeSampler sampler = make_finite_tpe(space, 41, 0, CandidatePolicy::WithoutReplacement);

    const auto first = sampler.ask();
    require(first.has_value(), "without-replacement TPE produced no first candidate");
    const std::uint64_t abandoned_ordinal = space.candidate_ordinal(*first);
    sampler.abandon(first->id);

    std::set<std::uint64_t> ordinals{abandoned_ordinal};
    while (const auto candidate = sampler.ask()) {
        const std::uint64_t ordinal = space.candidate_ordinal(*candidate);
        require(ordinals.insert(ordinal).second,
                "abandoned or pending finite candidate was proposed again");
        sampler.abandon(candidate->id);
    }
    require(ordinals.size() == 4, "without-replacement TPE did not exhaust the 2x2 space");
    require(sampler.generated() == 4 && sampler.completed() == 0 && sampler.outstanding() == 0,
            "abandoned finite-candidate counters are incorrect");

    TpeSampler limited = make_finite_tpe(space, 73, 3, CandidatePolicy::WithoutReplacement);
    std::set<std::uint64_t> limited_ordinals;
    for (std::uint64_t index = 0; index < 3; ++index) {
        const auto candidate = limited.ask();
        require(candidate.has_value(), "limited without-replacement TPE ended early");
        require(limited_ordinals.insert(space.candidate_ordinal(*candidate)).second,
                "limited without-replacement TPE repeated a candidate");
        limited.abandon(candidate->id);
    }
    require(!limited.ask().has_value(), "without-replacement TPE exceeded max_candidates");

    require_throws<std::invalid_argument>(
        [&]() { (void)make_finite_tpe(space, 73, 5, CandidatePolicy::WithoutReplacement); },
        "without-replacement TPE accepted a budget above cardinality");
    require_throws<std::invalid_argument>(
        [&]() { (void)make_finite_tpe(space, 73, 3, CandidatePolicy::Exhaustive); },
        "exhaustive TPE accepted a budget below cardinality");
}

void test_without_replacement_reset_replay() {
    using namespace pineforge::hpo;

    const SearchSpace space = make_two_by_two_space();
    TpeSampler sampler = make_finite_tpe(space, 109, 4, CandidatePolicy::WithoutReplacement);

    auto run_abandoned_sequence = [&]() {
        std::vector<std::uint64_t> ordinals;
        for (std::uint64_t index = 0; index < 4; ++index) {
            const auto candidate = sampler.ask();
            require(candidate.has_value(), "reset replay sequence ended early");
            ordinals.push_back(space.candidate_ordinal(*candidate));
            sampler.abandon(candidate->id);
        }
        require(!sampler.ask().has_value(), "reset replay sequence did not exhaust finite space");
        return ordinals;
    };

    const auto original = run_abandoned_sequence();
    sampler.reset();
    require(sampler.generated() == 0 && sampler.completed() == 0 && sampler.outstanding() == 0 &&
                sampler.duplicate_proposals_skipped() == 0,
            "finite TPE reset did not clear lifecycle/proposal state");
    const auto replay = run_abandoned_sequence();
    require(replay == original, "finite TPE reset did not replay the seeded proposal sequence");
}

void test_fitted_tpe_exhausts_finite_space() {
    using namespace pineforge::hpo;

    const SearchSpace space({IntegerDimension("x", 0, 3), BooleanDimension("enabled"),
                             CategoricalDimension("mode", {std::string("a"), std::string("b")})});
    const auto cardinality = space.finite_cardinality();
    require(cardinality.has_value() && *cardinality == 16,
            "fitted-phase fixture cardinality is not 16");

    std::set<std::uint64_t> grid_ordinals;
    GridSampler grid(space);
    while (const auto candidate = grid.next()) {
        grid_ordinals.insert(space.candidate_ordinal(*candidate));
    }

    TpeSamplerConfig config;
    config.startup_trials = 2;
    config.ei_candidates = 8;
    TpeSampler sampler(space, 149, ObjectiveDirection::Maximize, *cardinality, config,
                       CandidatePolicy::Exhaustive);

    std::set<std::uint64_t> tpe_ordinals;
    for (std::uint64_t index = 0; index < *cardinality; ++index) {
        const auto candidate = sampler.ask();
        require(candidate.has_value(), "fitted finite TPE ended before space exhaustion");
        const std::uint64_t ordinal = space.candidate_ordinal(*candidate);
        require(tpe_ordinals.insert(ordinal).second,
                "fitted finite TPE repeated a reserved ordinal");

        const auto x = std::get<std::int64_t>(*candidate->find("x"));
        const bool enabled = std::get<bool>(*candidate->find("enabled"));
        const auto& mode = std::get<std::string>(*candidate->find("mode"));
        const double objective = -std::abs(static_cast<double>(x) - 2.0) + (enabled ? 0.5 : 0.0) +
                                 (mode == "b" ? 0.25 : 0.0);
        sampler.tell(candidate->id, objective);
    }

    require(!sampler.ask().has_value(), "fitted finite TPE exceeded exact cardinality");
    require(tpe_ordinals == grid_ordinals,
            "fitted finite TPE did not cover the same candidate set as GridSampler");
    require(sampler.generated() == *cardinality && sampler.completed() == *cardinality &&
                sampler.outstanding() == 0,
            "fitted finite TPE lifecycle counters are incorrect at exhaustion");
}

void test_hundred_million_candidate_space_is_lazy() {
    using namespace pineforge::hpo;

    constexpr std::uint64_t kCardinality = 100'000'000;
    constexpr std::uint64_t kBudget = 64;
    const SearchSpace space({IntegerDimension("x", 0, kCardinality - 1)});
    require(space.finite_cardinality() == kCardinality,
            "100-million-candidate fixture cardinality drifted");

    TpeSamplerConfig config;
    config.startup_trials = 4;
    config.ei_candidates = 8;
    TpeSampler sampler(space, 20260718, ObjectiveDirection::Maximize, kBudget, config,
                       CandidatePolicy::WithoutReplacement);
    std::set<std::uint64_t> ordinals;
    for (std::uint64_t trial = 0; trial < kBudget; ++trial) {
        const auto candidate = sampler.ask();
        require(candidate.has_value(), "large finite space ended before its small trial budget");
        const std::uint64_t ordinal = space.candidate_ordinal(*candidate);
        require(ordinals.insert(ordinal).second,
                "large finite space repeated a candidate inside its trial budget");
        sampler.tell(candidate->id, -std::abs(static_cast<double>(ordinal) - 12'345'678.0));
    }
    require(!sampler.ask().has_value() && ordinals.size() == kBudget,
            "large finite space did not stop at its requested unique budget");
}

}  // namespace

int main() {
    try {
        test_mixed_radix_rank_round_trip();
        test_real_grid_endpoint_semantics();
        test_fixed_and_continuous_real_semantics();
        test_cardinality_and_abi_rejections();
        test_exhaustive_tpe_matches_grid_before_tell();
        test_without_replacement_lifecycle_and_limits();
        test_without_replacement_reset_replay();
        test_fitted_tpe_exhausts_finite_space();
        test_hundred_million_candidate_space_is_lazy();
        std::cout << "Finite search-space tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Finite search-space test failure: " << error.what() << '\n';
        return 1;
    }
}
