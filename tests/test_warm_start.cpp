#include <pineforge/hpo/sampler.hpp>
#include "../src/cli/continuation.hpp"

#include <cmath>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <vector>

namespace pfh = pineforge::hpo;

namespace {

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

double score(const pfh::Candidate& candidate) {
    const auto value = std::get<double>(candidate.values.at("x"));
    return -std::abs(value - 0.37);
}

void replay(std::uint64_t batch, bool bounded, pfh::CandidatePolicy policy) {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0, 0.001)});
    pfh::TpeSamplerConfig config;
    if (bounded)
        config.history_switch = 32;
    pfh::TpeSampler uninterrupted(space, 73, pfh::ObjectiveDirection::Maximize, 0, config, policy);
    std::vector<pfh::WarmStartObservation> warm;
    for (std::uint64_t begin = 0; begin < 200; begin += batch) {
        std::vector<pfh::Candidate> pending;
        for (std::uint64_t index = 0; index < batch; ++index) {
            const auto candidate = uninterrupted.ask();
            require(candidate.has_value(), "parent proposal missing");
            pending.push_back(*candidate);
        }
        for (const auto& candidate : pending) {
            const bool failed = candidate.id % 19 == 0;
            const auto objective = failed ? std::nullopt : std::optional<double>(score(candidate));
            warm.push_back({candidate, objective});
            if (objective)
                uninterrupted.tell(candidate.id, *objective);
            else
                uninterrupted.abandon(candidate.id);
        }
    }
    pfh::TpeSampler continued(space, 73, pfh::ObjectiveDirection::Maximize, 200, config, policy);
    require(continued.warm_start(warm, batch), "complete batches did not replay exactly");
    require(continued.generated() == 0, "warm observations consumed new budget");
    require(continued.duplicate_proposals_skipped() == 0, "warm skip count leaked into new job");
    require(continued.completed() == uninterrupted.completed(), "completed count differs");
    require(continued.outstanding() == 0, "warm observations left pending candidates");
    require(continued.retained_observations() == uninterrupted.retained_observations(),
            "bounded warm history differs");
    for (std::uint64_t begin = 0; begin < 200; begin += batch) {
        std::vector<pfh::Candidate> pending;
        for (std::uint64_t index = 0; index < batch; ++index) {
            const auto expected = uninterrupted.ask();
            const auto actual = continued.ask();
            require(expected && actual, "continuation proposal missing");
            require(expected->id == actual->id && expected->values == actual->values,
                    "uninterrupted and continuation proposals differ");
            pending.push_back(*actual);
        }
        for (const auto& candidate : pending) {
            uninterrupted.tell(candidate.id, score(candidate));
            continued.tell(candidate.id, score(candidate));
        }
    }
    require(!continued.ask(), "new continuation budget ignored");
}

void rebuilt_history() {
    const pfh::SearchSpace space({pfh::IntegerDimension("x", 0, 999)});
    pfh::TpeSamplerConfig config;
    config.history_switch = 20;
    config.startup_trials = 5;
    config.bad_reservoir_size = 7;
    std::vector<pfh::WarmStartObservation> warm;
    for (std::uint64_t identifier = 0; identifier < 200; ++identifier) {
        pfh::Candidate candidate;
        candidate.id = identifier * 2;
        candidate.values.emplace("x", static_cast<std::int64_t>(identifier));
        warm.push_back({candidate, identifier % 2 ? std::nullopt :
            std::optional<double>(static_cast<double>(identifier))});
    }
    pfh::TpeSampler first(space, 41, pfh::ObjectiveDirection::Minimize, 25, config,
                          pfh::CandidatePolicy::WithoutReplacement);
    pfh::TpeSampler second(space, 41, pfh::ObjectiveDirection::Minimize, 25, config,
                           pfh::CandidatePolicy::WithoutReplacement);
    require(!first.warm_start(warm, 8), "non-contiguous history incorrectly replayed");
    require(!second.warm_start(warm, 5), "non-contiguous history incorrectly replayed");
    require(first.completed() == 100 && first.outstanding() == 0, "failed trials trained TPE");
    require(first.retained_observations() <= config.gamma_cap + 64 + config.bad_reservoir_size,
            "warm observations bypassed history switch");
    std::set<std::int64_t> seen;
    for (const auto& observation : warm)
        seen.insert(std::get<std::int64_t>(observation.candidate.values.at("x")));
    for (std::uint64_t index = 0; index < 25; ++index) {
        const auto left = first.ask();
        const auto right = second.ask();
        require(left && right && left->values == right->values,
                "rebuilt history is not replayable");
        require(left->id == 399 + index, "warm IDs did not continue after highest ID");
        require(seen.insert(std::get<std::int64_t>(left->values.at("x"))).second,
                "without_replacement repeated an attempted vector");
        first.abandon(left->id);
        second.abandon(right->id);
    }
    require(!first.ask(), "new budget exceeded");
}

void invalid_imports() {
    const pfh::SearchSpace space({pfh::IntegerDimension("x", 0, 10)});
    const pfh::Candidate valid{3, {{"x", std::int64_t{2}}}};
    for (const auto& warm : std::vector<std::vector<pfh::WarmStartObservation>>{
        {{valid, 1.0}, {valid, 2.0}},
        {{pfh::Candidate{0, {{"x", std::int64_t{11}}}}, 1.0}},
        {{valid, std::numeric_limits<double>::infinity()}},
        {{pfh::Candidate{std::numeric_limits<std::uint64_t>::max(), valid.values}, 1.0}}}) {
        pfh::TpeSampler sampler(space, 1);
        bool rejected = false;
        try {
            sampler.warm_start(warm);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        require(rejected && sampler.generated() == 0 && sampler.completed() == 0,
                "invalid warm import mutated sampler");
    }
}

}

int main() {
    try {
        require(pfh::detail::sha256("") ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "SHA empty");
        require(pfh::detail::sha256("abc") ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "SHA abc");
        const auto golden = pfh::detail::recorded_space(
            pfh::SearchSpace({pfh::RealDimension("Entry Level", 101, 103, 1),
                             pfh::RealDimension("Exit Level", 98, 100, 1)}),
            "metrics.all.net_profit - 0.5 * metrics.equity.max_equity_drawdown", "maximize",
            {"metrics.all.num_trades >= 1"});
        require(pfh::detail::space_hash(golden) ==
            "1188c07588652f2b365a1fc233815d5c51d350e92106051309d9e84ad586071a",
            "space hash golden changed");
        const auto mixed = pfh::detail::recorded_space(pfh::SearchSpace({
            pfh::IntegerDimension("count", -2, 4, 2), pfh::BooleanDimension("enabled"),
            pfh::CategoricalDimension("choice", {std::int64_t{3}, 4.0, true,
                                                std::string("臺北\n")}),
            pfh::RealDimension("scale", 0.1, 10.0, std::nullopt, true)}),
            "metrics.all.net_profit", "minimize", {"2 > 1", "1 > 0"});
        require(pfh::detail::space_hash(mixed) ==
            "fd04b34677f03f8d4d2f49cccb1c58ca42ea9fcc448bbcd05ab72d052c5ecb90",
            "mixed typed/Unicode space hash golden changed");
        for (const auto batch : {2, 5, 8}) {
            for (const bool bounded : {false, true}) {
                replay(batch, bounded, pfh::CandidatePolicy::SamplerDefault);
                replay(batch, bounded, pfh::CandidatePolicy::WithoutReplacement);
            }
        }
        rebuilt_history();
        invalid_imports();
        std::cout << "PASS: SHA/golden, replay and uninterrupted equivalence (batches 2/5/8), "
                     "bounded warm history, startup, IDs, finite reservations "
                     "and invalid imports\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
