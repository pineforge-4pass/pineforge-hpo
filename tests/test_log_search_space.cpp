#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>

#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

template <typename Function>
void require_invalid_argument(Function&& function, const std::string& expected) {
    try {
        function();
    } catch (const std::invalid_argument& error) {
        require(std::string(error.what()).find(expected) != std::string::npos,
                "unexpected invalid_argument: " + std::string(error.what()));
        return;
    }
    throw std::runtime_error("expected invalid_argument containing: " + expected);
}

void test_log_dimension_contracts() {
    using namespace pineforge::hpo;

    const IntegerDimension integer("period", 1, 1'000'000, 1, true);
    require(integer.log(), "log integer metadata was lost");
    require(integer.step() == 1, "log integer step changed");

    const RealDimension real("rate", 1e-9, 1e3, std::nullopt, true);
    require(real.log(), "log real metadata was lost");
    require(!real.step().has_value(), "log real acquired a step");

    require_invalid_argument([]() { IntegerDimension invalid("period", 0, 10, 1, true); },
                             "bounds must be positive");
    require_invalid_argument([]() { IntegerDimension invalid("period", 1, 10, 2, true); },
                             "requires step = 1");
    require_invalid_argument(
        []() { RealDimension invalid("rate", -1.0, 10.0, std::nullopt, true); },
        "bounds must be positive");
    require_invalid_argument([]() { RealDimension invalid("rate", 1.0, 10.0, 0.1, true); },
                             "does not support a step");
}

void test_grid_contract() {
    using namespace pineforge::hpo;

    GridSampler integers(SearchSpace({IntegerDimension("period", 1, 4, 1, true)}));
    for (std::int64_t expected = 1; expected <= 4; ++expected) {
        const auto candidate = integers.next();
        require(candidate.has_value(), "log integer grid ended early");
        require(std::get<std::int64_t>(*candidate->find("period")) == expected,
                "log integer grid did not retain finite-grid order");
    }
    require(!integers.next().has_value(), "log integer grid produced an extra value");

    require_invalid_argument(
        []() {
            GridSampler invalid(
                SearchSpace({RealDimension("rate", 1e-3, 1e3, std::nullopt, true)}));
        },
        "requires a step");
}

void test_random_log_uniform_and_reset() {
    using namespace pineforge::hpo;

    const SearchSpace space({IntegerDimension("period", 1, 1'000'000, 1, true),
                             RealDimension("rate", 1e-6, 1e6, std::nullopt, true)});
    RandomSampler sampler(space, 0x5eedU, 20'000);
    std::array<std::uint64_t, 6> integer_decades{};
    std::array<std::uint64_t, 6> real_bands{};
    std::int64_t first_integer = 0;
    double first_real = 0.0;

    for (std::uint64_t index = 0; index < 20'000; ++index) {
        const auto candidate = sampler.next();
        require(candidate.has_value(), "random log sampler ended early");
        require(space.is_valid(*candidate), "random log sampler produced an invalid candidate");

        const auto integer = std::get<std::int64_t>(*candidate->find("period"));
        const auto real = std::get<double>(*candidate->find("rate"));
        if (index == 0) {
            first_integer = integer;
            first_real = real;
        }

        const auto integer_band = static_cast<std::size_t>(
            std::min(5.0, std::floor(std::log10(static_cast<double>(integer)))));
        ++integer_decades[integer_band];
        const auto real_band =
            static_cast<std::size_t>(std::min(5.0, std::floor((std::log10(real) + 6.0) / 2.0)));
        ++real_bands[real_band];
    }

    // A linear-uniform sampler would overwhelmingly occupy the final band.
    // These broad deterministic bounds verify that the latent coordinate is
    // uniform in log space without turning a statistical smoke test brittle.
    for (std::size_t index = 0; index < integer_decades.size(); ++index) {
        require(integer_decades[index] > 2'500 && integer_decades[index] < 4'200,
                "log integer samples were not spread across decades");
        require(real_bands[index] > 2'500 && real_bands[index] < 4'200,
                "log real samples were not spread across log bands");
    }

    sampler.reset();
    const auto repeated = sampler.next();
    require(repeated.has_value(), "reset random log sampler produced no candidate");
    require(std::get<std::int64_t>(*repeated->find("period")) == first_integer,
            "random log integer did not replay after reset");
    require(std::get<double>(*repeated->find("rate")) == first_real,
            "random log real did not replay after reset");

    const auto maximum = std::numeric_limits<std::int64_t>::max();
    const SearchSpace adjacent_space({IntegerDimension("period", maximum - 1, maximum, 1, true)});
    RandomSampler adjacent(adjacent_space, 73, 256);
    bool saw_low = false;
    bool saw_high = false;
    while (const auto candidate = adjacent.next()) {
        const auto value = std::get<std::int64_t>(*candidate->find("period"));
        saw_low = saw_low || value == maximum - 1;
        saw_high = saw_high || value == maximum;
    }
    require(saw_low && saw_high,
            "random log integer lost adjacent values near the int64 upper bound");
}

void test_dlib_log_coordinates() {
    using namespace pineforge::hpo;

    const SearchSpace space({IntegerDimension("period", 1, 1'000'000, 1, true),
                             RealDimension("rate", 1e-9, 1e3, std::nullopt, true)});
    DlibGlobalSampler first(space, 91, ObjectiveDirection::Minimize, 32);
    DlibGlobalSampler second(space, 91, ObjectiveDirection::Minimize, 32);

    for (std::uint64_t index = 0; index < 32; ++index) {
        const auto left = first.ask();
        const auto right = second.ask();
        require(left.has_value() && right.has_value(), "dlib log sampler ended early");
        require(left->values == right->values, "dlib log coordinates were not reproducible");
        require(space.is_valid(*left), "dlib log decoder produced an invalid candidate");

        const auto period = std::get<std::int64_t>(*left->find("period"));
        const auto rate = std::get<double>(*left->find("rate"));
        const double objective =
            std::pow(std::log(static_cast<double>(period)) - std::log(3'000.0), 2.0) +
            std::pow(std::log(rate) - std::log(0.02), 2.0);
        first.tell(left->id, objective);
        second.tell(right->id, objective);
    }
    require(first.completed() == 32 && first.outstanding() == 0,
            "dlib log request lifecycle counters drifted");

    require_invalid_argument(
        []() {
            const auto high = std::numeric_limits<std::int64_t>::max();
            DlibGlobalSampler invalid(
                SearchSpace({IntegerDimension("period", high - 1, high, 1, true)}), 1);
        },
        "bounds collapse in double precision");
}

double log_objective(const pineforge::hpo::Candidate& candidate) {
    const auto period = std::get<std::int64_t>(*candidate.find("period"));
    const auto rate = std::get<double>(*candidate.find("rate"));
    return std::pow(std::log(static_cast<double>(period)) - std::log(2'000.0), 2.0) +
           std::pow(std::log(rate) - std::log(0.02), 2.0);
}

void test_tpe_log_coordinates_and_quality() {
    using namespace pineforge::hpo;

    const SearchSpace space({IntegerDimension("period", 1, 1'000'000, 1, true),
                             RealDimension("rate", 1e-9, 1e3, std::nullopt, true)});
    TpeSamplerConfig config;
    config.startup_trials = 10;
    config.ei_candidates = 48;
    config.constant_liar = true;
    TpeSampler first(space, 20260718, ObjectiveDirection::Minimize, 160, config);
    TpeSampler second(space, 20260718, ObjectiveDirection::Minimize, 160, config);
    double best = std::numeric_limits<double>::infinity();

    for (std::uint64_t index = 0; index < 160; ++index) {
        const auto left = first.ask();
        const auto right = second.ask();
        require(left.has_value() && right.has_value(), "TPE log sampler ended early");
        require(left->values == right->values, "TPE log proposals were not reproducible");
        require(space.is_valid(*left), "TPE log decoder produced an invalid candidate");
        const double objective = log_objective(*left);
        best = std::min(best, objective);
        first.tell(left->id, objective);
        second.tell(right->id, objective);
    }

    require(best < 0.1, "TPE failed to refine a smooth nonlinear objective in log coordinates");
    require(first.completed() == 160 && first.outstanding() == 0,
            "TPE log request lifecycle counters drifted");
}

void test_tpe_high_range_relative_coordinates() {
    using namespace pineforge::hpo;

    TpeSamplerConfig startup_config;
    startup_config.startup_trials = 600;

    const std::int64_t high_integer = std::numeric_limits<std::int64_t>::max();
    const SearchSpace integer_space(
        {IntegerDimension("period", high_integer - 1'000'000, high_integer, 1, true)});
    TpeSampler integer_sampler(integer_space, 321, ObjectiveDirection::Minimize, 512,
                               startup_config);
    std::set<std::int64_t> integer_values;
    for (std::uint64_t index = 0; index < 512; ++index) {
        const auto candidate = integer_sampler.ask();
        require(candidate.has_value(), "high-range log-integer sampler ended early");
        require(integer_space.is_valid(*candidate),
                "high-range log-integer sampler produced an invalid candidate");
        const auto value = std::get<std::int64_t>(*candidate->find("period"));
        integer_values.insert(value);
        integer_sampler.tell(candidate->id, static_cast<double>(high_integer - value));
    }
    require(integer_values.size() > 480,
            "high-range log-integer decoder collapsed distinct legal values");

    const double high_real = 1e300;
    const double low_real = high_real * (1.0 - 1e-12);
    const SearchSpace real_space({RealDimension("rate", low_real, high_real, std::nullopt, true)});
    TpeSampler real_sampler(real_space, 322, ObjectiveDirection::Minimize, 512, startup_config);
    std::set<double> real_values;
    for (std::uint64_t index = 0; index < 512; ++index) {
        const auto candidate = real_sampler.ask();
        require(candidate.has_value(), "high-range log-real sampler ended early");
        require(real_space.is_valid(*candidate),
                "high-range log-real sampler produced an invalid candidate");
        const double value = std::get<double>(*candidate->find("rate"));
        real_values.insert(value);
        real_sampler.tell(candidate->id, (high_real - value) / (high_real - low_real));
    }
    require(real_values.size() > 450, "high-range log-real decoder collapsed representable values");

    // With one observation, changing EI from one draw to twenty-four must
    // permit the likelihood ratio to choose a different candidate.  The old
    // absolute-log implementation produced zero-width bins, NaN ratios, and
    // silently returned the first draw for both configurations.
    TpeSamplerConfig one_ei;
    one_ei.startup_trials = 1;
    one_ei.ei_candidates = 1;
    TpeSamplerConfig many_ei = one_ei;
    many_ei.ei_candidates = 24;
    TpeSampler first(integer_space, 123, ObjectiveDirection::Maximize, 2, one_ei);
    TpeSampler many(integer_space, 123, ObjectiveDirection::Maximize, 2, many_ei);
    const auto first_startup = first.ask();
    const auto many_startup = many.ask();
    require(first_startup->values == many_startup->values,
            "high-range log-integer startup replay drifted");
    first.tell(first_startup->id, 1.0);
    many.tell(many_startup->id, 1.0);
    const auto first_modeled = first.ask();
    const auto many_modeled = many.ask();
    require(first_modeled->values != many_modeled->values,
            "high-range log-integer EI ranking degenerated to its first draw");
    first.abandon(first_modeled->id);
    many.abandon(many_modeled->id);

    // Cardinality alone is insufficient for a logarithmic discrete domain:
    // every rounding bin must also remain distinct in normalized double space.
    require_invalid_argument(
        []() {
            TpeSampler invalid(
                SearchSpace({IntegerDimension("period", 1, std::int64_t{1} << 53, 1, true)}), 1);
        },
        "cannot represent every discrete bin");
}

}  // namespace

int main() {
    try {
        test_log_dimension_contracts();
        test_grid_contract();
        test_random_log_uniform_and_reset();
        test_dlib_log_coordinates();
        test_tpe_log_coordinates_and_quality();
        test_tpe_high_range_relative_coordinates();
        std::cout << "log search-space tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "log search-space test failure: " << error.what() << '\n';
        return 1;
    }
}
