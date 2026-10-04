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

void checkpoint_equivalence(std::uint64_t batch, bool bounded, pfh::CandidatePolicy policy,
                            std::uint64_t history_count) {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0, 0.001)});
    pfh::TpeSamplerConfig config;
    if (bounded)
        config.history_switch = 32;
    pfh::TpeSampler uninterrupted(space, 73, pfh::ObjectiveDirection::Maximize, 0, config, policy);
    std::vector<pfh::WarmStartObservation> warm;
    for (std::uint64_t begin = 0; begin < history_count; begin += batch) {
        std::vector<pfh::Candidate> pending;
        for (std::uint64_t index = 0; index < std::min(batch, history_count - begin); ++index) {
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
    require(continued.warm_start(warm, batch, uninterrupted.sampler_state()),
            "checkpoint did not restore exactly");
    require(continued.generated() == 0, "warm observations consumed new budget");
    require(continued.duplicate_proposals_skipped() == 0, "warm skip count leaked into new job");
    require(continued.completed() == uninterrupted.completed(), "completed count differs");
    require(continued.outstanding() == 0, "warm observations left pending candidates");
    require(continued.retained_observations() == uninterrupted.retained_observations(),
            "bounded warm history differs");
    for (std::uint64_t begin = 0; begin < 200; begin += batch) {
        std::vector<pfh::Candidate> pending;
        for (std::uint64_t index = 0; index < std::min(batch, 200 - begin); ++index) {
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

void checkpoint_validation() {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0)});
    pfh::TpeSampler parent(space, 17);
    std::vector<pfh::WarmStartObservation> warm;
    for (std::uint64_t index = 0; index < 33; ++index) {
        const auto candidate = *parent.ask();
        parent.tell(candidate.id, score(candidate));
        warm.push_back({candidate, score(candidate)});
    }
    const auto state = parent.sampler_state();
    pfh::TpeSampler legacy(space, 17);
    require(!legacy.warm_start(warm, 1), "legacy history generated historical proposals");
    pfh::TpeSampler different_seed(space, 18);
    require(!different_seed.warm_start(warm, 1, state), "different seed restored a checkpoint");
    warm.front().objective = 42;
    pfh::TpeSampler changed_history(space, 17);
    require(!changed_history.warm_start(warm, 1, state), "changed history restored a checkpoint");
    pfh::TpeSampler corrupt(space, 17);
    bool rejected = false;
    try {
        corrupt.warm_start(warm, 1, state + "corrupt");
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected && corrupt.generated() == 0 && corrupt.completed() == 0,
            "corrupt checkpoint mutated the sampler");
    const auto pending = *parent.ask();
    rejected = false;
    try {
        parent.sampler_state();
    } catch (const std::logic_error&) {
        rejected = true;
    }
    require(rejected, "checkpoint allowed outstanding candidates");
    parent.abandon(pending.id);
    parent.reset();
    pfh::TpeSampler initial(space, 17);
    require(parent.sampler_state() == initial.sampler_state(), "reset left checkpoint state");
}

void parallel_checkpoint_equivalence() {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0),
        pfh::RealDimension("y", 0.01, 10.0, std::nullopt, true),
        pfh::IntegerDimension("period", 1, 10000),
        pfh::CategoricalDimension("mode", {std::string("a"), std::string("b")}),
        pfh::BooleanDimension("enabled")});
    std::vector<pfh::WarmStartObservation> warm;
    for (std::uint64_t identifier = 0; identifier < 4200; ++identifier) {
        pfh::Candidate candidate;
        candidate.id = identifier;
        candidate.values = {{"x", static_cast<double>(identifier % 997) / 997.0},
            {"y", 0.01 + static_cast<double>(identifier % 991) / 100.0},
            {"period", static_cast<std::int64_t>(identifier + 1)},
            {"mode", std::string(identifier % 2 ? "a" : "b")},
            {"enabled", identifier % 2 != 0}};
        warm.push_back({candidate, static_cast<double>(identifier % 101)});
    }
    pfh::TpeSampler parent(space, 73);
    parent.warm_start(warm);
    pfh::TpeSampler child(space, 73);
    require(child.warm_start(warm, 8, parent.sampler_state()),
            "full-history parallel checkpoint did not restore");
    std::vector<pfh::Candidate> pending;
    for (std::uint64_t index = 0; index < 8; ++index) {
        const auto expected = *parent.ask();
        const auto actual = *child.ask();
        require(expected.id == actual.id && expected.values == actual.values,
                "parallel fit/scoring changed checkpoint suggestions");
        pending.push_back(actual);
    }
    for (const auto& candidate : pending) {
        parent.tell(candidate.id, static_cast<double>(candidate.id % 101));
        child.tell(candidate.id, static_cast<double>(candidate.id % 101));
    }
}

void linear_history_import() {
    class CountingSource final : public pfh::WarmStartSource {
    public:
        explicit CountingSource(std::uint64_t count) : count_(count) {}
        std::uint64_t size() const noexcept override { return count_; }
        std::uint64_t id(std::uint64_t row) const override { return row; }
        pfh::ParameterValue parameter(std::uint64_t row, std::size_t column) const override {
            ++reads;
            return static_cast<double>((row + column) % 997) / 997.0;
        }
        std::optional<double> objective(std::uint64_t row) const override {
            return static_cast<double>(row % 101);
        }
        mutable std::uint64_t reads = 0;
    private:
        std::uint64_t count_;
    };
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0),
                                  pfh::RealDimension("y", 0.0, 1.0)});
    for (const auto count : {31U, 1000U, 8000U}) {
        for (const auto batch : {1U, 8U, 31U}) {
            for (const auto seed : {7U, 73U}) {
                auto source = std::make_shared<CountingSource>(count);
                pfh::TpeSampler sampler(space, seed);
                require(!sampler.warm_start(source, batch), "row-only import restored state");
                require(source->reads == 4 * count, "import fitted or replayed historical models");
                require(sampler.generated() == 0 && sampler.completed() == count,
                        "import changed candidate accounting");
            }
        }
    }
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
        for (const auto batch : {1, 2, 4, 5, 8, 32}) {
            for (const bool bounded : {false, true}) {
                for (const auto history : {31, 64, 201}) {
                    checkpoint_equivalence(batch, bounded, pfh::CandidatePolicy::SamplerDefault,
                                           history);
                    checkpoint_equivalence(batch, bounded, pfh::CandidatePolicy::WithoutReplacement,
                                           history);
                }
            }
        }
        checkpoint_validation();
        parallel_checkpoint_equivalence();
        linear_history_import();
        rebuilt_history();
        invalid_imports();
        std::cout << "PASS: SHA/golden, checkpoint/uninterrupted equivalence, partial batches, "
                     "bounded warm history, startup, IDs, finite reservations "
                     "and invalid imports\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
