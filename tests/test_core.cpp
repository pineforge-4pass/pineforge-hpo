#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "pineforge/hpo/objective.hpp"
#include "pineforge/hpo/portfolio.hpp"
#include "pineforge/hpo/sampler.hpp"
#include "pineforge/hpo/search_space.hpp"
#include "pineforge/hpo/types.hpp"

namespace {

int failures = 0;

#define CHECK(condition)                                                                       \
    do {                                                                                       \
        if (!(condition)) {                                                                    \
            std::cerr << __FILE__ << ':' << __LINE__ << ": check failed: " #condition << '\n'; \
            ++failures;                                                                        \
        }                                                                                      \
    } while (false)

template <typename Exception, typename Function>
void check_throws(Function&& function) {
    bool threw_expected = false;
    try {
        function();
    } catch (const Exception&) {
        threw_expected = true;
    } catch (...) {}
    CHECK(threw_expected);
}

pineforge::hpo::SearchSpace make_space() {
    using namespace pineforge::hpo;
    SearchSpace space;
    space.add(IntegerDimension("length", 1, 3));
    space.add(RealDimension("risk", 0.5, 1.0, 0.5));
    space.add(BooleanDimension("enabled"));
    space.add(CategoricalDimension(
        "mode", std::vector<ParameterValue>{std::string("ema"), std::string("sma")}));
    return space;
}

void test_types_and_search_space() {
    using namespace pineforge::hpo;

    CHECK(parameter_type(ParameterValue(std::int64_t{7})) == ParameterType::Integer);
    CHECK(parameter_type(ParameterValue(0.5)) == ParameterType::Real);
    CHECK(parameter_type(ParameterValue(true)) == ParameterType::Boolean);
    CHECK(parameter_type(ParameterValue(std::string("ema"))) == ParameterType::String);
    CHECK(serialize_parameter_value(ParameterValue(std::int64_t{-7})) == "-7");
    CHECK(serialize_parameter_value(ParameterValue(0.5)) == "0.5");
    CHECK(serialize_parameter_value(ParameterValue(true)) == "true");
    CHECK(serialize_parameter_value(ParameterValue(std::string("ema"))) == "ema");
    check_throws<std::invalid_argument>(
        [] { serialize_parameter_value(ParameterValue(std::numeric_limits<double>::infinity())); });

    check_throws<std::invalid_argument>([] { IntegerDimension("bad", 3, 1); });
    check_throws<std::invalid_argument>([] { IntegerDimension("bad", 1, 3, 0); });
    check_throws<std::invalid_argument>([] { RealDimension("bad", 0.0, 1.0, -0.1); });
    check_throws<std::invalid_argument>(
        [] { CategoricalDimension("bad", std::vector<ParameterValue>{}); });
    check_throws<std::invalid_argument>([] {
        CategoricalDimension("bad",
                             std::vector<ParameterValue>{std::string("x"), std::string("x")});
    });

    SearchSpace space = make_space();
    check_throws<std::invalid_argument>([&] { space.add(BooleanDimension("enabled")); });

    Candidate candidate;
    candidate.id = 12;
    candidate.values = {{"length", std::int64_t{2}},
                        {"risk", 1.0},
                        {"enabled", true},
                        {"mode", std::string("sma")}};
    CHECK(candidate.find("length") != nullptr);
    CHECK(candidate.find("missing") == nullptr);
    CHECK(space.is_valid(candidate));

    const auto serialized = space.serialize_candidate(candidate);
    CHECK(serialized.at("length") == "2");
    CHECK(serialized.at("risk") == "1");
    CHECK(serialized.at("enabled") == "true");
    CHECK(serialized.at("mode") == "sma");

    candidate.values["length"] = std::int64_t{4};
    candidate.values.erase("mode");
    candidate.values["unknown"] = true;
    const auto issues = space.validate(candidate);
    CHECK(issues.size() == 3);
    CHECK(!space.is_valid(candidate));
    check_throws<std::invalid_argument>([&] { space.serialize_candidate(candidate); });
}

void test_grid_sampler() {
    using namespace pineforge::hpo;

    SearchSpace space = make_space();
    GridSampler sampler(space);
    CHECK(sampler.total_candidates() == 24);
    CHECK(sampler.generated() == 0);

    std::optional<Candidate> first;
    std::optional<Candidate> last;
    std::uint64_t count = 0;
    while (auto candidate = sampler.next()) {
        CHECK(space.is_valid(*candidate));
        CHECK(candidate->id == count);
        if (count == 0) {
            first = candidate;
        }
        last = candidate;
        ++count;
    }
    CHECK(count == 24);
    CHECK(!sampler.next().has_value());
    CHECK(std::get<std::int64_t>(*first->find("length")) == 1);
    CHECK(std::get<double>(*first->find("risk")) == 0.5);
    CHECK(!std::get<bool>(*first->find("enabled")));
    CHECK(std::get<std::string>(*first->find("mode")) == "ema");
    CHECK(std::get<std::int64_t>(*last->find("length")) == 3);
    CHECK(std::get<double>(*last->find("risk")) == 1.0);
    CHECK(std::get<bool>(*last->find("enabled")));
    CHECK(std::get<std::string>(*last->find("mode")) == "sma");

    sampler.reset();
    CHECK(sampler.generated() == 0);
    CHECK(sampler.next()->values == first->values);

    SearchSpace continuous;
    continuous.add(RealDimension("real", 0.0, 1.0));
    check_throws<std::invalid_argument>([&] { GridSampler invalid(continuous); });

    GridSampler empty(SearchSpace{});
    CHECK(empty.total_candidates() == 1);
    CHECK(empty.next()->values.empty());
    CHECK(!empty.next().has_value());
}

void test_random_sampler() {
    using namespace pineforge::hpo;

    const SearchSpace space = make_space();
    RandomSampler first(space, 20260718, 32);
    RandomSampler second(space, 20260718, 32);
    for (std::uint64_t i = 0; i < 32; ++i) {
        const auto left = first.next();
        const auto right = second.next();
        CHECK(left.has_value());
        CHECK(right.has_value());
        CHECK(left->id == i);
        CHECK(left->values == right->values);
        CHECK(space.is_valid(*left));
    }
    CHECK(!first.next().has_value());
    first.reset();
    second.reset();
    CHECK(first.next()->values == second.next()->values);

    SearchSpace continuous;
    continuous.add(RealDimension("learning_rate", 1e-5, 1e-1));
    RandomSampler real_sampler(continuous, 9, 20);
    while (auto candidate = real_sampler.next()) {
        CHECK(continuous.is_valid(*candidate));
    }
}

double tpe_mixed_objective(const pineforge::hpo::Candidate& candidate) {
    const auto length = static_cast<double>(std::get<std::int64_t>(*candidate.find("length")));
    const double continuous = std::get<double>(*candidate.find("continuous"));
    const double stepped = std::get<double>(*candidate.find("stepped"));
    const bool enabled = std::get<bool>(*candidate.find("enabled"));
    const auto& mode = std::get<std::string>(*candidate.find("mode"));
    return -0.25 * std::pow(length - 7.0, 2.0) - std::pow(continuous - 0.75, 2.0) -
           std::pow(stepped - 0.5, 2.0) + (enabled ? 1.0 : 0.0) + (mode == "sma" ? 2.0 : 0.0);
}

pineforge::hpo::SearchSpace make_tpe_space() {
    using namespace pineforge::hpo;
    SearchSpace space;
    space.add(IntegerDimension("length", 1, 9, 2));
    space.add(RealDimension("continuous", -4.0, 4.0));
    space.add(RealDimension("stepped", -1.0, 1.0, 0.25));
    space.add(BooleanDimension("enabled"));
    space.add(CategoricalDimension(
        "mode",
        std::vector<ParameterValue>{std::string("ema"), std::string("sma"), std::string("wma")}));
    return space;
}

void test_tpe_sampler() {
    using namespace pineforge::hpo;

    TpeSamplerConfig config;
    config.startup_trials = 8;
    config.ei_candidates = 32;
    config.gamma_fraction = 0.20;
    config.gamma_cap = 12;
    config.prior_weight = 1.0;
    config.constant_liar = true;

    const SearchSpace space = make_tpe_space();
    TpeSampler first(space, 20260718, ObjectiveDirection::Maximize, 96, config);
    TpeSampler second(space, 20260718, ObjectiveDirection::Maximize, 96, config);
    CHECK(first.seed() == 20260718);
    CHECK(first.direction() == ObjectiveDirection::Maximize);
    CHECK(first.config().ei_candidates == 32);

    double best = -std::numeric_limits<double>::infinity();
    for (std::uint64_t i = 0; i < 96; ++i) {
        const auto left = first.ask();
        const auto right = second.next();
        CHECK(left.has_value());
        CHECK(right.has_value());
        CHECK(left->id == i);
        CHECK(left->values == right->values);
        CHECK(space.is_valid(*left));
        const double score = tpe_mixed_objective(*left);
        best = std::max(best, score);
        first.tell(left->id, score);
        second.tell(right->id, score);
    }
    CHECK(!first.ask().has_value());
    CHECK(first.generated() == 96);
    CHECK(first.completed() == 96);
    CHECK(first.outstanding() == 0);
    CHECK(best > 2.95);

    first.reset();
    second.reset();
    CHECK(first.generated() == 0);
    CHECK(first.completed() == 0);
    CHECK(first.outstanding() == 0);
    CHECK(first.ask()->values == second.ask()->values);

    // Pending trials are not completed observations.  With constant-liar
    // enabled they contribute parameter values only to the bad estimator.
    TpeSampler concurrent(space, 77, ObjectiveDirection::Maximize, 20, config);
    for (std::uint64_t i = 0; i < config.startup_trials; ++i) {
        const auto candidate = concurrent.ask();
        concurrent.tell(candidate->id, tpe_mixed_objective(*candidate));
    }
    std::vector<Candidate> batch;
    for (std::uint64_t i = 0; i < 4; ++i) {
        const auto candidate = concurrent.ask();
        CHECK(candidate.has_value());
        CHECK(space.is_valid(*candidate));
        batch.push_back(*candidate);
    }
    CHECK(concurrent.completed() == config.startup_trials);
    CHECK(concurrent.outstanding() == 4);
    concurrent.tell(batch[3].id, tpe_mixed_objective(batch[3]));
    concurrent.tell(batch[1].id, tpe_mixed_objective(batch[1]));
    concurrent.abandon(batch[0].id);
    CHECK(concurrent.completed() == config.startup_trials + 2);
    CHECK(concurrent.outstanding() == 1);
    check_throws<std::invalid_argument>([&] { concurrent.abandon(batch[0].id); });
    check_throws<std::invalid_argument>(
        [&] { concurrent.tell(batch[2].id, std::numeric_limits<double>::quiet_NaN()); });
    CHECK(concurrent.outstanding() == 1);
    check_throws<std::invalid_argument>(
        [&] { concurrent.tell(batch[2].id, std::numeric_limits<double>::infinity()); });
    CHECK(concurrent.outstanding() == 1);
    check_throws<std::invalid_argument>(
        [&] { concurrent.tell(batch[2].id, -std::numeric_limits<double>::infinity()); });
    CHECK(concurrent.outstanding() == 1);
    concurrent.abandon(batch[2].id);
    check_throws<std::invalid_argument>([&] { concurrent.tell(9999, 1.0); });

    // reset() must not reuse candidate IDs while an earlier request can still
    // report a result.  Rejecting the reset keeps the pending request intact.
    TpeSampler reset_guard(space, 78, ObjectiveDirection::Maximize, 4, config);
    const auto before_reset = reset_guard.ask();
    check_throws<std::logic_error>([&] { reset_guard.reset(); });
    CHECK(reset_guard.generated() == 1);
    CHECK(reset_guard.completed() == 0);
    CHECK(reset_guard.outstanding() == 1);
    reset_guard.abandon(before_reset->id);
    reset_guard.reset();
    CHECK(reset_guard.generated() == 0);
    CHECK(reset_guard.completed() == 0);
    CHECK(reset_guard.outstanding() == 0);

    // A positive prior can still underflow in an extreme categorical model.
    // The sampler must surface the resulting non-finite acquisition instead of
    // silently selecting the first EI draw.
    TpeSamplerConfig underflow_config = config;
    underflow_config.startup_trials = 2;
    underflow_config.ei_candidates = 4;
    underflow_config.prior_weight = std::numeric_limits<double>::denorm_min();
    underflow_config.constant_liar = false;
    SearchSpace binary;
    binary.add(BooleanDimension("flag"));
    bool exercised_non_finite_acquisition = false;
    for (std::uint64_t seed = 1; seed <= 64; ++seed) {
        TpeSampler underflow(binary, seed, ObjectiveDirection::Maximize, 3, underflow_config);
        const auto good = underflow.ask();
        const auto bad = underflow.ask();
        if (good->values == bad->values) {
            underflow.abandon(good->id);
            underflow.abandon(bad->id);
            continue;
        }
        underflow.tell(good->id, 1.0);
        underflow.tell(bad->id, 0.0);
        check_throws<std::logic_error>([&] { (void)underflow.ask(); });
        CHECK(underflow.generated() == 2);
        CHECK(underflow.completed() == 2);
        CHECK(underflow.outstanding() == 0);
        exercised_non_finite_acquisition = true;
        break;
    }
    CHECK(exercised_non_finite_acquisition);

    // Minimize(f) and maximize(-f) normalize to identical scores and therefore
    // generate the same deterministic sequence.
    SearchSpace continuous;
    continuous.add(RealDimension("x", -5.0, 5.0));
    TpeSampler minimize(continuous, 91, ObjectiveDirection::Minimize, 40, config);
    TpeSampler maximize_negative(continuous, 91, ObjectiveDirection::Maximize, 40, config);
    for (std::uint64_t i = 0; i < 40; ++i) {
        const auto left = minimize.ask();
        const auto right = maximize_negative.ask();
        CHECK(left->values == right->values);
        const double x = std::get<double>(*left->find("x"));
        const double value = std::pow(x - 0.25, 2.0);
        minimize.tell(left->id, value);
        maximize_negative.tell(right->id, -value);
    }

    // Lock the sparse-model path: with one observation, its bandwidth is the
    // larger distance to a domain endpoint.  The midpoint prior is appended
    // afterward and must not act as an observation neighbor.
    SearchSpace sparse_space;
    sparse_space.add(RealDimension("x", 0.0, 1.0));
    TpeSamplerConfig sparse_config = config;
    sparse_config.startup_trials = 1;
    sparse_config.ei_candidates = 1;
    TpeSampler sparse(sparse_space, 12345, ObjectiveDirection::Maximize, 2, sparse_config);
    const auto sparse_startup = sparse.ask();
    CHECK(std::abs(std::get<double>(*sparse_startup->find("x")) - 0.35762972288842587) < 1e-14);
    sparse.tell(sparse_startup->id, 1.0);
    const auto sparse_modeled = sparse.ask();
    CHECK(std::abs(std::get<double>(*sparse_modeled->find("x")) - 0.53532613059242296) < 1e-13);

    TpeSamplerConfig invalid = config;
    invalid.startup_trials = 0;
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });
    invalid = config;
    invalid.ei_candidates = 0;
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });
    invalid.ei_candidates = 1'000'001;
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });
    invalid = config;
    invalid.gamma_fraction = std::numeric_limits<double>::quiet_NaN();
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });
    invalid = config;
    invalid.gamma_fraction = 1.01;
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });
    invalid = config;
    invalid.gamma_cap = 0;
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });
    invalid = config;
    invalid.prior_weight = 0.0;
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(space, 1, ObjectiveDirection::Maximize, 0, invalid); });

    SearchSpace too_wide;
    too_wide.add(IntegerDimension("x", std::numeric_limits<std::int64_t>::min(),
                                  std::numeric_limits<std::int64_t>::max()));
    check_throws<std::overflow_error>(
        [&] { TpeSampler sampler(too_wide, 1, ObjectiveDirection::Maximize, 1, config); });

    constexpr std::int64_t kExactlyRepresentableCardinality = std::int64_t{1} << 53;
    SearchSpace boundary;
    boundary.add(IntegerDimension("x", 0, kExactlyRepresentableCardinality - 1));
    TpeSampler boundary_sampler(boundary, 18, ObjectiveDirection::Maximize, 6, config);
    for (std::uint64_t i = 0; i < 6; ++i) {
        const auto candidate = boundary_sampler.ask();
        CHECK(candidate.has_value());
        CHECK(boundary.is_valid(*candidate));
        const auto value = std::get<std::int64_t>(*candidate->find("x"));
        boundary_sampler.tell(candidate->id, -static_cast<double>(value));
    }

    SearchSpace beyond_boundary;
    beyond_boundary.add(IntegerDimension("x", 0, kExactlyRepresentableCardinality));
    check_throws<std::invalid_argument>(
        [&] { TpeSampler sampler(beyond_boundary, 1, ObjectiveDirection::Maximize, 1, config); });

    // Empty and all-fixed spaces contain exactly one unique candidate.  They
    // must not schedule duplicate backtests even with an unbounded limit.
    TpeSampler empty(SearchSpace{}, 3, ObjectiveDirection::Maximize, 0, config);
    const auto empty_candidate = empty.ask();
    CHECK(empty_candidate.has_value());
    CHECK(empty_candidate->values.empty());
    CHECK(!empty.ask().has_value());
    empty.tell(empty_candidate->id, 1.0);
    CHECK(empty.completed() == 1);

    SearchSpace fixed;
    fixed.add(IntegerDimension("integer", 5, 5));
    fixed.add(RealDimension("real", 0.25, 0.25));
    fixed.add(CategoricalDimension("category", std::vector<ParameterValue>{std::string("only")}));
    TpeSampler fixed_sampler(fixed, 4, ObjectiveDirection::Maximize, 100, config);
    const auto fixed_candidate = fixed_sampler.ask();
    CHECK(fixed_candidate.has_value());
    CHECK(fixed.is_valid(*fixed_candidate));
    CHECK(!fixed_sampler.ask().has_value());
    fixed_sampler.abandon(fixed_candidate->id);
    CHECK(!fixed_sampler.ask().has_value());

    // Exercise overflow-safe transforms at both the widest finite range and a
    // one-ULP range.  Every sampled/re-encoded value must remain finite/legal.
    const double maximum = std::numeric_limits<double>::max();
    SearchSpace extreme;
    extreme.add(RealDimension("wide", -maximum, maximum));
    extreme.add(RealDimension("positive", maximum / 2.0, maximum));
    extreme.add(RealDimension("narrow", 1.0, std::nextafter(1.0, 2.0)));
    TpeSampler extreme_sampler(extreme, 811, ObjectiveDirection::Maximize, 64, config);
    for (std::uint64_t i = 0; i < 64; ++i) {
        const auto candidate = extreme_sampler.ask();
        CHECK(candidate.has_value());
        CHECK(extreme.is_valid(*candidate));
        const double wide = std::get<double>(*candidate->find("wide"));
        const double positive = std::get<double>(*candidate->find("positive"));
        const double narrow = std::get<double>(*candidate->find("narrow"));
        CHECK(std::isfinite(wide));
        CHECK(std::isfinite(positive));
        CHECK(std::isfinite(narrow));
        const double objective = -std::abs(wide / maximum) - positive / maximum - narrow;
        CHECK(std::isfinite(objective));
        extreme_sampler.tell(candidate->id, objective);
    }

    // Repeated endpoint objectives drive narrow tail/bin calculations.  Both
    // ends must remain reachable without endpoint half-mass bias or -inf/NaN.
    SearchSpace discrete;
    discrete.add(IntegerDimension("x", 0, 8));
    TpeSampler low_endpoint(discrete, 991, ObjectiveDirection::Maximize, 80, config);
    TpeSampler high_endpoint(discrete, 992, ObjectiveDirection::Maximize, 80, config);
    bool found_low = false;
    bool found_high = false;
    for (std::uint64_t i = 0; i < 80; ++i) {
        const auto low_candidate = low_endpoint.ask();
        const auto high_candidate = high_endpoint.ask();
        const auto low_value = std::get<std::int64_t>(*low_candidate->find("x"));
        const auto high_value = std::get<std::int64_t>(*high_candidate->find("x"));
        found_low = found_low || low_value == 0;
        found_high = found_high || high_value == 8;
        low_endpoint.tell(low_candidate->id, -static_cast<double>(low_value));
        high_endpoint.tell(high_candidate->id, static_cast<double>(high_value));
    }
    CHECK(found_low);
    CHECK(found_high);
}

void test_tpe_log_sampler() {
    using namespace pineforge::hpo;

    const SearchSpace space({IntegerDimension("period", 1, 1'000'000, 1, true),
                             RealDimension("rate", 1e-9, 1e3, std::nullopt, true)});
    TpeSamplerConfig config;
    config.startup_trials = 10;
    config.ei_candidates = 48;
    config.gamma_fraction = 0.10;
    config.gamma_cap = 25;
    config.prior_weight = 1.0;
    config.constant_liar = true;

    TpeSampler first(space, 0x51a7U, ObjectiveDirection::Minimize, 160, config);
    TpeSampler replay(space, 0x51a7U, ObjectiveDirection::Minimize, 160, config);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t index = 0; index < 160; ++index) {
        const auto candidate = first.ask();
        const auto repeated = replay.ask();
        CHECK(candidate.has_value());
        CHECK(repeated.has_value());
        CHECK(candidate->values == repeated->values);
        CHECK(space.is_valid(*candidate));

        const double period =
            static_cast<double>(std::get<std::int64_t>(*candidate->find("period")));
        const double rate = std::get<double>(*candidate->find("rate"));
        const double objective =
            std::pow(std::log(period / 3'000.0), 2.0) + std::pow(std::log(rate / 0.02), 2.0);
        best = std::min(best, objective);
        first.tell(candidate->id, objective);
        replay.tell(repeated->id, objective);
    }
    CHECK(best < 0.10);
    CHECK(first.completed() == 160);
    CHECK(first.outstanding() == 0);

    // The full finite positive double range remains representable after the
    // logarithmic transform and every decoded proposal is legal.
    SearchSpace extreme({RealDimension("x", std::numeric_limits<double>::min(),
                                       std::numeric_limits<double>::max(), std::nullopt, true)});
    TpeSampler extreme_sampler(extreme, 44, ObjectiveDirection::Minimize, 32, config);
    for (std::uint64_t index = 0; index < 32; ++index) {
        const auto candidate = extreme_sampler.ask();
        CHECK(candidate.has_value());
        CHECK(extreme.is_valid(*candidate));
        const double value = std::get<double>(*candidate->find("x"));
        extreme_sampler.tell(candidate->id, std::abs(std::log(value)));
    }
}

double dlib_mixed_objective(const pineforge::hpo::Candidate& candidate) {
    const auto length = static_cast<double>(std::get<std::int64_t>(*candidate.find("length")));
    const double risk = std::get<double>(*candidate.find("risk"));
    const bool enabled = std::get<bool>(*candidate.find("enabled"));
    const auto& mode = std::get<std::string>(*candidate.find("mode"));
    return -std::pow(length - 3.0, 2.0) - std::pow(risk - 1.0, 2.0) + (enabled ? 1.0 : 0.0) +
           (mode == "sma" ? 2.0 : 0.0);
}

void test_dlib_sampler() {
    using namespace pineforge::hpo;

    const SearchSpace space = make_space();
    check_throws<std::invalid_argument>([&] {
        DlibGlobalSampler invalid_seed(
            space, static_cast<std::uint64_t>(std::numeric_limits<std::int32_t>::max()) + 1);
    });
    DlibGlobalSampler sampler(space, 20260718, ObjectiveDirection::Maximize, 24);
    CHECK(sampler.seed() == 20260718);
    CHECK(sampler.direction() == ObjectiveDirection::Maximize);

    // dlib permits multiple outstanding requests.  Report this batch in
    // reverse order to exercise the external scheduler's ask/tell contract.
    std::vector<Candidate> batch;
    for (std::uint64_t i = 0; i < 4; ++i) {
        auto candidate = sampler.ask();
        CHECK(candidate.has_value());
        CHECK(candidate->id == i);
        CHECK(space.is_valid(*candidate));
        batch.push_back(std::move(*candidate));
    }
    CHECK(sampler.generated() == 4);
    CHECK(sampler.outstanding() == 4);
    for (auto candidate = batch.rbegin(); candidate != batch.rend(); ++candidate) {
        sampler.tell(candidate->id, dlib_mixed_objective(*candidate));
    }
    CHECK(sampler.completed() == 4);
    CHECK(sampler.outstanding() == 0);

    while (auto candidate = sampler.next()) {
        CHECK(space.is_valid(*candidate));
        sampler.tell(candidate->id, dlib_mixed_objective(*candidate));
    }
    CHECK(sampler.generated() == 24);
    CHECK(sampler.completed() == 24);
    CHECK(!sampler.ask().has_value());

    check_throws<std::invalid_argument>([&] { sampler.tell(999, 1.0); });

    DlibGlobalSampler cancellation(space, 7, ObjectiveDirection::Maximize, 2);
    const auto cancelled = cancellation.ask();
    CHECK(cancelled.has_value());
    check_throws<std::invalid_argument>(
        [&] { cancellation.tell(cancelled->id, std::numeric_limits<double>::quiet_NaN()); });
    CHECK(cancellation.outstanding() == 1);
    cancellation.abandon(cancelled->id);
    CHECK(cancellation.outstanding() == 0);
    CHECK(cancellation.completed() == 0);
    check_throws<std::invalid_argument>([&] { cancellation.abandon(cancelled->id); });

    // Minimize(f) and maximize(-f) must produce the same model updates and
    // therefore the same deterministic candidate sequence.
    SearchSpace continuous;
    continuous.add(RealDimension("x", -4.0, 4.0));
    DlibGlobalSampler minimize(continuous, 91, ObjectiveDirection::Minimize, 20);
    DlibGlobalSampler maximize_negative(continuous, 91, ObjectiveDirection::Maximize, 20);
    double best_minimize = std::numeric_limits<double>::infinity();
    for (std::uint64_t i = 0; i < 20; ++i) {
        const auto left = minimize.ask();
        const auto right = maximize_negative.ask();
        CHECK(left.has_value());
        CHECK(right.has_value());
        CHECK(left->values == right->values);
        const double x = std::get<double>(*left->find("x"));
        const double value = std::pow(x - 0.75, 2.0);
        best_minimize = std::min(best_minimize, value);
        minimize.tell(left->id, value);
        maximize_negative.tell(right->id, -value);
    }
    CHECK(best_minimize < 1e-4);

    const auto initial = minimize.generated();
    CHECK(initial == 20);
    minimize.reset();
    maximize_negative.reset();
    CHECK(minimize.generated() == 0);
    CHECK(minimize.completed() == 0);
    CHECK(minimize.ask()->values == maximize_negative.ask()->values);

    // A search space containing only fixed dimensions still represents one
    // valid trial even though dlib has no non-constant coordinate to optimize.
    SearchSpace fixed;
    fixed.add(IntegerDimension("integer", 5, 5));
    fixed.add(RealDimension("real", 0.25, 0.25));
    fixed.add(CategoricalDimension("category", std::vector<ParameterValue>{std::string("only")}));
    DlibGlobalSampler fixed_sampler(fixed, 3, ObjectiveDirection::Maximize, 10);
    const auto only = fixed_sampler.ask();
    CHECK(only.has_value());
    CHECK(fixed.is_valid(*only));
    CHECK(!fixed_sampler.ask().has_value());
    fixed_sampler.tell(only->id, 1.0);
    CHECK(fixed_sampler.completed() == 1);
}

void test_metric_expression() {
    using namespace pineforge::hpo;

    MetricMap metrics{{"a", 10.0}, {"b", 3.0}, {"metrics.all.num_trades", 2.0}};
    MetricExpression expression("max(a / 2, abs(-b)) + min(4, b)");
    const auto score = expression.evaluate(metrics);
    CHECK(score.valid);
    CHECK(score.value == 8.0);
    CHECK(expression.identifiers().size() == 2);

    MetricExpression precedence("1 + 2 * 3 == 7");
    CHECK(precedence.evaluate(metrics).value == 1.0);
    MetricExpression constraint("metrics.all.num_trades >= 1");
    CHECK(constraint.evaluate(metrics).value == 1.0);
    metrics["metrics.all.num_trades"] = 0.0;
    CHECK(constraint.evaluate(metrics).value == 0.0);
    MetricExpression less_equal("b <= 3");
    CHECK(less_equal.evaluate(metrics).value == 1.0);
    MetricExpression less("b < 3");
    CHECK(less.evaluate(metrics).value == 0.0);
    MetricExpression comparisons("a > b + 6 != 0");
    CHECK(comparisons.evaluate(metrics).value == 1.0);
    MetricExpression exponent("+1e2 / (2 * 5)");
    CHECK(exponent.evaluate(metrics).value == 10.0);

    MetricExpression missing("missing + 1");
    const auto missing_result = missing.evaluate(metrics);
    CHECK(!missing_result.valid);
    CHECK(missing_result.error == EvaluationError::MissingMetric);

    MetricExpression divide("a / zero");
    metrics["zero"] = 0.0;
    const auto divide_result = divide.evaluate(metrics);
    CHECK(!divide_result.valid);
    CHECK(divide_result.error == EvaluationError::DivisionByZero);

    EvaluationPolicy ieee_policy;
    ieee_policy.division_by_zero = DivisionByZeroPolicy::Ieee754;
    ieee_policy.non_finite_result = NonFinitePolicy::Allow;
    const auto ieee_result = divide.evaluate(metrics, ieee_policy);
    CHECK(ieee_result.valid);
    CHECK(std::isinf(ieee_result.value));

    metrics["nan"] = std::numeric_limits<double>::quiet_NaN();
    MetricExpression non_finite("nan");
    CHECK(non_finite.evaluate(metrics).error == EvaluationError::NonFiniteMetric);
    EvaluationPolicy allow_metric_only;
    allow_metric_only.non_finite_metric = NonFinitePolicy::Allow;
    CHECK(non_finite.evaluate(metrics, allow_metric_only).error ==
          EvaluationError::NonFiniteResult);
    EvaluationPolicy allow_nan;
    allow_nan.non_finite_metric = NonFinitePolicy::Allow;
    allow_nan.non_finite_result = NonFinitePolicy::Allow;
    CHECK(non_finite.evaluate(metrics, allow_nan).valid);

    check_throws<ExpressionError>([] { MetricExpression invalid("unknown(1)"); });
    check_throws<ExpressionError>([] { MetricExpression invalid("min(1)"); });
    check_throws<ExpressionError>([] { MetricExpression invalid("1 +"); });
    check_throws<ExpressionError>([] { MetricExpression invalid("1 = 1"); });
}

void test_constraints_and_portfolio_objective() {
    using namespace pineforge::hpo;

    Constraint drawdown{"max_drawdown", 0.18, ConstraintRelation::LessEqual, 0.20, 0.0};
    CHECK(drawdown.satisfied());
    CHECK(drawdown.violation() == 0.0);
    Constraint concentration{"max_weight", 0.60, ConstraintRelation::LessEqual, 0.50, 0.0};
    CHECK(!concentration.satisfied());
    CHECK(std::abs(concentration.violation() - 0.10) < 1e-12);

    PortfolioObservation observation;
    observation.account_equity = {{1, 100.0}, {2, 110.0}};
    observation.allocations = {{"trend", "btc", 0.4}, {"mean", "eth", 0.6}};
    observation.turnover = 0.25;
    observation.capital_used = 1.0;

    PortfolioObjectiveFn objective = [](const PortfolioObservation& portfolio,
                                        const TrialContext&) {
        const double total_return =
            portfolio.account_equity.back().equity / portfolio.account_equity.front().equity - 1.0;
        ObjectiveResult result;
        result.values = {total_return - 0.1 * portfolio.turnover};
        result.constraints.push_back(
            {"capital", portfolio.capital_used, ConstraintRelation::LessEqual, 1.0, 1e-12});
        return result;
    };

    Candidate candidate;
    candidate.id = 7;
    TrialContext context{candidate.id, &candidate};
    const ObjectiveResult result = objective(observation, context);
    CHECK(result.valid);
    CHECK(result.feasible());
    CHECK(result.values.size() == 1);
    CHECK(std::abs(result.values[0] - 0.075) < 1e-12);
    CHECK(!ObjectiveResult::invalid("failed").feasible());
}

}  // namespace

int main() {
    test_types_and_search_space();
    test_grid_sampler();
    test_random_sampler();
    test_tpe_sampler();
    test_tpe_log_sampler();
    test_dlib_sampler();
    test_metric_expression();
    test_constraints_and_portfolio_objective();

    if (failures != 0) {
        std::cerr << failures << " core test(s) failed\n";
        return 1;
    }
    std::cout << "all core tests passed\n";
    return 0;
}
