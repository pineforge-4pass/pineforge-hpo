#include <pineforge/hpo/sampler.hpp>
#include "../src/cli/continuation.hpp"
#include "../src/core/tpe_test_hooks.hpp"

#include <cmath>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <vector>

namespace pfh = pineforge::hpo;

namespace {

thread_local std::vector<double>* observed_log_ratios = nullptr;

void observe_log_ratio(double value) {
    observed_log_ratios->push_back(value);
}

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

double score(const pfh::Candidate& candidate) {
    const auto value = std::get<double>(candidate.values.at("x"));
    return -std::abs(value - 0.37);
}

void checkpoint_equivalence(std::uint64_t batch, bool bounded, pfh::CandidatePolicy policy,
                            std::uint64_t history_count, std::uint64_t reservoir = 448,
                            std::uint64_t new_trials = 200) {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0, 0.001)});
    pfh::TpeSamplerConfig config;
    if (bounded)
        config.history_switch = 32;
    config.bad_reservoir_size = reservoir;
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
    pfh::TpeSampler continued(space, 73, pfh::ObjectiveDirection::Maximize, new_trials,
                             config, policy);
    require(continued.warm_start(warm, batch, uninterrupted.sampler_state()),
            "checkpoint did not restore exactly");
    require(continued.generated() == 0, "warm observations consumed new budget");
    require(continued.duplicate_proposals_skipped() == 0, "warm skip count leaked into new job");
    require(continued.completed() == uninterrupted.completed(), "completed count differs");
    require(continued.outstanding() == 0, "warm observations left pending candidates");
    require(continued.retained_observations() == uninterrupted.retained_observations(),
            "bounded warm history differs");
    for (std::uint64_t begin = 0; begin < new_trials; begin += batch) {
        std::vector<pfh::Candidate> pending;
        for (std::uint64_t index = 0; index < std::min(batch, new_trials - begin); ++index) {
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

std::string sealed_state(const std::string& payload) {
    return "PFHTPE2\n" + pfh::detail::sha256(payload) + '\n' + payload;
}

void state_mismatch_and_transition() {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0)});
    pfh::TpeSamplerConfig config;
    config.history_switch = 128;
    config.bad_reservoir_size = 7;
    pfh::TpeSampler parent(space, 17, pfh::ObjectiveDirection::Maximize, 0, config);
    std::vector<pfh::WarmStartObservation> warm;
    for (std::size_t index = 0; index < 128; ++index) {
        const auto candidate = *parent.ask();
        parent.tell(candidate.id, score(candidate));
        warm.push_back({candidate, score(candidate)});
    }
    const auto state = parent.sampler_state();
    require(state.size() < 15000 && state.substr(state.size() - 6) == "0 \n0 \n",
            "transition checkpoint included full-history model IDs");
    pfh::TpeSampler child(space, 17, pfh::ObjectiveDirection::Maximize, 0, config);
    require(child.warm_start(warm, 1, state), "transition checkpoint did not restore");
    require(parent.ask()->values == child.ask()->values, "transition checkpoint changed proposals");
    auto payload = state.substr(73);
    const auto build_begin = payload.find('\n') + 1;
    const auto build_end = payload.find('\n', build_begin);
    payload.replace(build_begin, build_end - build_begin, "\"foreign-stdlib/fp-build\"");
    pfh::TpeSampler foreign(space, 17, pfh::ObjectiveDirection::Maximize, 0, config);
    require(!foreign.warm_start(warm, 1, sealed_state(payload)),
            "foreign numerical build did not rebuild");
    auto shorter = warm;
    shorter.resize(12);
    pfh::TpeSampler mismatch(space, 17, pfh::ObjectiveDirection::Maximize, 0, config);
    require(!mismatch.warm_start(shorter, 1, state), "short history did not rebuild");
    payload = state.substr(73);
    const auto engine_begin = payload.find("MT64 312 ");
    const auto engine_end = payload.find('\n', engine_begin);
    std::ostringstream zero;
    zero << "MT64 312 ";
    for (std::size_t index = 0; index < 312; ++index)
        zero << "0 ";
    zero << "312";
    payload.replace(engine_begin, engine_end - engine_begin, zero.str());
    pfh::TpeSampler corrupt(space, 17, pfh::ObjectiveDirection::Maximize, 0, config);
    bool rejected = false;
    try {
        corrupt.warm_start(warm, 1, sealed_state(payload));
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected && corrupt.completed() == 0, "forged zero MT state accepted/mutated sampler");
}

std::string serial_golden_identity() {
    return "portable-tpe-v2;binary64:53";
}

void check_serial_golden(const std::string& identity, const std::string& expected,
                         const std::string& actual) {
    if (actual != expected)
        throw std::runtime_error("serial golden mismatch for key " + identity +
                                 ": expected " + expected + ", got " + actual);
}

void serial_golden_mismatch_diagnostic() {
    const auto identity = serial_golden_identity();
    bool rejected = false;
    try {
        check_serial_golden(identity, "expected-hash", "actual-hash");
    } catch (const std::runtime_error& error) {
        rejected = std::string(error.what()) == "serial golden mismatch for key " + identity +
                   ": expected expected-hash, got actual-hash";
    }
    require(rejected, "serial golden mismatch did not identify its precision key and hashes");
    std::cout << "PASS serial golden mismatch names key " << identity << '\n';
}

bool parallel_checkpoint_equivalence() {
    serial_golden_mismatch_diagnostic();
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
    pfh::TpeSamplerConfig serial_config;
    serial_config.max_threads = 1;
    auto parallel_config = serial_config;
    parallel_config.max_threads = 8;
    pfh::TpeSampler parent(space, 73, pfh::ObjectiveDirection::Maximize, 0, serial_config);
    parent.warm_start(warm);
    pfh::TpeSampler child(space, 73, pfh::ObjectiveDirection::Maximize, 0, parallel_config);
    require(child.warm_start(warm, 8, parent.sampler_state()),
            "full-history parallel checkpoint did not restore");
    std::vector<pfh::Candidate> pending;
    std::ostringstream bits;
    for (std::uint64_t index = 0; index < 8; ++index) {
        const auto expected = *parent.ask();
        const auto actual = *child.ask();
        require(expected.id == actual.id && expected.values == actual.values,
                "parallel fit/scoring changed checkpoint suggestions");
        for (const auto& item : actual.values) {
            bits << item.first << ':' << item.second.index() << ':';
            if (const auto* value = std::get_if<double>(&item.second)) {
                std::uint64_t raw;
                std::memcpy(&raw, value, sizeof(raw));
                std::uint64_t expected_raw;
                const auto expected_value = std::get<double>(expected.values.at(item.first));
                std::memcpy(&expected_raw, &expected_value, sizeof(expected_raw));
                require(raw == expected_raw, "threaded/serial double bits differ");
                bits << raw;
            } else {
                std::visit([&](const auto& value) { bits << value; }, item.second);
            }
            bits << ' ';
        }
        pending.push_back(actual);
    }
    const auto identity = serial_golden_identity();
    std::cout << "Serial golden numeric identity: "
              << pfh::detail::tpe_numeric_identity(space) << "; golden key: " << identity << '\n';
    std::ifstream goldens(PFH_TPE_SERIAL_GOLDENS);
    require(static_cast<bool>(goldens), "serial golden identity table missing");
    std::string known_identity, golden_hash;
    bool matched = false;
    while (goldens >> known_identity >> golden_hash) {
        if (known_identity != identity)
            continue;
        const auto actual_hash = pfh::detail::sha256(bits.str());
        check_serial_golden(identity, golden_hash, actual_hash);
        matched = true;
        break;
    }
    if (!matched)
        std::cout << "SKIP independent serial golden: unknown numerical identity "
                  << identity << "; proposal SHA-256 " << pfh::detail::sha256(bits.str()) << '\n';
    else
        std::cout << "PASS independent serial golden " << identity << '\n';
    for (const auto& candidate : pending) {
        parent.tell(candidate.id, static_cast<double>(candidate.id % 101));
        child.tell(candidate.id, static_cast<double>(candidate.id % 101));
    }
    require(matched || !std::getenv("PFH_REQUIRE_SERIAL_GOLDEN"),
            "independent serial golden is required for this CI build");
    return matched;
}

long double changed_log1p(long double value) {
    return std::nextafter(std::log1p(value), std::numeric_limits<long double>::infinity());
}

void scoped_numeric_identity_validation() {
    for (const pfh::SearchSpace space : {
        pfh::SearchSpace({pfh::RealDimension("value", 0.0, 1.0)}),
        pfh::SearchSpace({pfh::IntegerDimension("value", 1, 99)}),
        pfh::SearchSpace({pfh::CategoricalDimension("value", {std::string("a"),
                                                           std::string("b")})}),
        pfh::SearchSpace({pfh::RealDimension("value", 0.1, 99.0, std::nullopt, true)}),
        pfh::SearchSpace({pfh::IntegerDimension("value", 1, 99, 1, true)})}) {
        pfh::detail::set_tpe_long_log1p_probe(nullptr);
        const auto identity = pfh::detail::tpe_numeric_identity(space);
        pfh::TpeSampler parent(space, 17);
        const auto state = parent.sampler_state();
        pfh::detail::set_tpe_long_log1p_probe(changed_log1p);
        const auto changed = pfh::detail::tpe_numeric_identity(space);
        const bool logarithmic = std::visit([](const auto& dimension) {
            using Item = std::decay_t<decltype(dimension)>;
            if constexpr (std::is_same_v<Item, pfh::RealDimension> ||
                          std::is_same_v<Item, pfh::IntegerDimension>)
                return dimension.log();
            return false;
        }, space.dimensions().front());
        (void)logarithmic;
        require(identity == changed,
                "host long-double log1p changed the portable identity");
        pfh::TpeSampler child(space, 17);
        require(child.warm_start(std::vector<pfh::WarmStartObservation>{}, 1, state),
                "portable checkpoint did not restore across changed host log1p");
    }
    pfh::detail::set_tpe_long_log1p_probe(nullptr);
    std::cout << "PASS portable math: all spaces restore across changed host log1p\n";
}

void eight_worker_equivalence() {
    std::vector<pfh::Dimension> dimensions;
    for (std::size_t column = 0; column < 32; ++column)
        dimensions.emplace_back(pfh::RealDimension("P" + std::to_string(column), 0.0, 1.0));
    const pfh::SearchSpace space(std::move(dimensions));
    std::vector<pfh::WarmStartObservation> warm;
    for (std::uint64_t row = 0; row < 4200; ++row) {
        pfh::Candidate candidate;
        candidate.id = row;
        for (std::uint64_t column = 0; column < 32; ++column)
            candidate.values.emplace("P" + std::to_string(column),
                static_cast<double>((row * 73 + column * 17) % 4099) / 4099);
        warm.push_back({std::move(candidate), static_cast<double>(row % 97)});
    }
    pfh::TpeSamplerConfig serial;
    serial.max_threads = 1;
    auto threaded = serial;
    threaded.max_threads = 8;
    pfh::TpeSampler reference(space, 17, pfh::ObjectiveDirection::Maximize, 0, serial);
    pfh::TpeSampler actual(space, 17, pfh::ObjectiveDirection::Maximize, 0, threaded);
    reference.warm_start(warm);
    actual.warm_start(warm);
    for (std::size_t proposal = 0; proposal < 8; ++proposal) {
        std::vector<double> serial_ratios, threaded_ratios;
        observed_log_ratios = &serial_ratios;
        pfh::detail::set_tpe_log_ratio_observer(observe_log_ratio);
        const auto expected = *reference.ask();
        observed_log_ratios = &threaded_ratios;
        const auto observed = *actual.ask();
        pfh::detail::set_tpe_log_ratio_observer(nullptr);
        observed_log_ratios = nullptr;
        require(!serial_ratios.empty() && serial_ratios.size() == threaded_ratios.size(),
                "acquisition log-ratio test hook did not observe all candidates");
        require(std::memcmp(serial_ratios.data(), threaded_ratios.data(),
                            serial_ratios.size() * sizeof(double)) == 0,
                "threaded log-ratio reduction bits differ");
        for (const auto& item : observed.values) {
            const auto expected_value = std::get<double>(expected.values.at(item.first));
            const auto observed_value = std::get<double>(item.second);
            require(std::memcmp(&expected_value, &observed_value, sizeof(double)) == 0,
                    "one/eight-worker 32-input double bits differ");
        }
    }
}

void numeric_identity_validation() {
    const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0)});
    std::vector<pfh::WarmStartObservation> warm;
    pfh::detail::set_tpe_contraction_override(0.0);
    pfh::TpeSampler parent(space, 17);
    parent.warm_start(warm);
    const auto state = parent.sampler_state();
    pfh::detail::set_tpe_contraction_override(-0x1p-104);
    pfh::TpeSampler foreign(space, 17);
    require(!foreign.warm_start(warm, 1, state),
            "contraction canary mismatch falsely restored checkpoint");
    pfh::detail::set_tpe_contraction_override(0.0);
    for (const std::string version : {"PFHTPE1\n", "PFHTPE3\n", "PFHTPE12\n"}) {
        pfh::TpeSampler different_version(space, 17);
        require(!different_version.warm_start(warm, 1, version + state.substr(8)),
                "other checkpoint version did not rebuild");
    }
    pfh::detail::set_tpe_contraction_override(std::nullopt);
    std::cout << "PASS contraction-mismatch and other-version rebuilds\n";
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

int main(int argc, char** argv) {
    try {
        if (argc == 2) {
            if (std::string(argv[1]) == "--numeric-identity") {
                std::cout << pfh::detail::sha256(pfh::detail::tpe_numeric_identity()) << '\n'
                          << pfh::detail::tpe_numeric_identity() << '\n';
                numeric_identity_validation();
                return 0;
            }
            if (std::string(argv[1]) == "--serial-golden")
                return parallel_checkpoint_equivalence() ? 0 : 77;
            if (std::string(argv[1]) == "--reduction") {
                eight_worker_equivalence();
                std::cout << "PASS all serial/threaded acquisition log-ratio bits\n";
                return 0;
            }
            checkpoint_equivalence(std::stoull(argv[1]), true,
                pfh::CandidatePolicy::SamplerDefault, 201, 7, 300);
            std::cout << "PASS reservoir r7 parent201 + new300 batch " << argv[1] << '\n';
            return 0;
        }
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
        for (const auto batch : {1, 5, 8})
            checkpoint_equivalence(batch, true, pfh::CandidatePolicy::SamplerDefault, 201, 7, 300);
        checkpoint_validation();
        numeric_identity_validation();
        scoped_numeric_identity_validation();
        state_mismatch_and_transition();
        parallel_checkpoint_equivalence();
        eight_worker_equivalence();
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
