#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <vector>

#include "pineforge/hpo/sampler.hpp"
#include "pineforge/hpo/search_space.hpp"
#include "pineforge/hpo/types.hpp"

namespace {

using pineforge::hpo::BooleanDimension;
using pineforge::hpo::Candidate;
using pineforge::hpo::CategoricalDimension;
using pineforge::hpo::IntegerDimension;
using pineforge::hpo::ObjectiveDirection;
using pineforge::hpo::ParameterValue;
using pineforge::hpo::RealDimension;
using pineforge::hpo::SearchSpace;
using pineforge::hpo::TpeSampler;
using pineforge::hpo::TpeSamplerConfig;

constexpr double kPi = 3.141592653589793238462643383279502884;
constexpr std::array<std::int64_t, 6> kMillionTarget{8, 1, 6, 3, 9, 4};

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

double real_value(const Candidate& candidate, const std::string& name) {
    const auto* value = candidate.find(name);
    require(value != nullptr, "missing real parameter " + name);
    return std::get<double>(*value);
}

std::int64_t integer_value(const Candidate& candidate, const std::string& name) {
    const auto* value = candidate.find(name);
    require(value != nullptr, "missing integer parameter " + name);
    return std::get<std::int64_t>(*value);
}

bool boolean_value(const Candidate& candidate, const std::string& name) {
    const auto* value = candidate.find(name);
    require(value != nullptr, "missing Boolean parameter " + name);
    return std::get<bool>(*value);
}

const std::string& string_value(const Candidate& candidate, const std::string& name) {
    const auto* value = candidate.find(name);
    require(value != nullptr, "missing categorical parameter " + name);
    return std::get<std::string>(*value);
}

struct Problem {
    std::string name;
    SearchSpace space;
    std::uint64_t default_trials = 0;
    double optimum = 0.0;
    Candidate known_optimum;
    std::function<double(const Candidate&)> objective;
};

Problem make_branin() {
    Problem problem;
    problem.name = "branin2";
    problem.space.add(RealDimension("x1", -5.0, 10.0));
    problem.space.add(RealDimension("x2", 0.0, 15.0));
    problem.default_trials = 128;
    problem.optimum = 0.39788735772973816;
    problem.known_optimum.values = {{"x1", -kPi}, {"x2", 12.275}};
    problem.objective = [](const Candidate& candidate) {
        const double x1 = real_value(candidate, "x1");
        const double x2 = real_value(candidate, "x2");
        const double b = 5.1 / (4.0 * kPi * kPi);
        const double c = 5.0 / kPi;
        const double t = 1.0 / (8.0 * kPi);
        return std::pow(x2 - b * x1 * x1 + c * x1 - 6.0, 2.0) + 10.0 * (1.0 - t) * std::cos(x1) +
               10.0;
    };
    return problem;
}

Problem make_hartmann3() {
    Problem problem;
    problem.name = "hartmann3";
    for (std::size_t index = 0; index < 3; ++index) {
        problem.space.add(RealDimension("x" + std::to_string(index + 1), 0.0, 1.0));
    }
    problem.default_trials = 192;
    problem.optimum = -3.8627797869493365;
    problem.known_optimum.values = {{"x1", 0.11461292}, {"x2", 0.55564907}, {"x3", 0.85254695}};
    problem.objective = [](const Candidate& candidate) {
        constexpr std::array<double, 4> alpha{1.0, 1.2, 3.0, 3.2};
        constexpr std::array<std::array<double, 3>, 4> a{{
            {{3.0, 10.0, 30.0}},
            {{0.1, 10.0, 35.0}},
            {{3.0, 10.0, 30.0}},
            {{0.1, 10.0, 35.0}},
        }};
        constexpr std::array<std::array<double, 3>, 4> p{{
            {{0.3689, 0.1170, 0.2673}},
            {{0.4699, 0.4387, 0.7470}},
            {{0.1091, 0.8732, 0.5547}},
            {{0.0381, 0.5743, 0.8828}},
        }};
        const std::array<double, 3> x{
            real_value(candidate, "x1"),
            real_value(candidate, "x2"),
            real_value(candidate, "x3"),
        };
        double result = 0.0;
        for (std::size_t i = 0; i < alpha.size(); ++i) {
            double exponent = 0.0;
            for (std::size_t j = 0; j < x.size(); ++j) {
                exponent += a[i][j] * std::pow(x[j] - p[i][j], 2.0);
            }
            result -= alpha[i] * std::exp(-exponent);
        }
        return result;
    };
    return problem;
}

Problem make_rosenbrock6() {
    Problem problem;
    problem.name = "rosenbrock6";
    for (std::size_t index = 0; index < 6; ++index) {
        const std::string name = "x" + std::to_string(index + 1);
        problem.space.add(RealDimension(name, -2.048, 2.048));
        problem.known_optimum.values.emplace(name, 1.0);
    }
    problem.default_trials = 512;
    problem.optimum = 0.0;
    problem.objective = [](const Candidate& candidate) {
        std::array<double, 6> x{};
        for (std::size_t index = 0; index < x.size(); ++index) {
            x[index] = real_value(candidate, "x" + std::to_string(index + 1));
        }
        double result = 0.0;
        for (std::size_t index = 0; index + 1 < x.size(); ++index) {
            result += 100.0 * std::pow(x[index + 1] - x[index] * x[index], 2.0) +
                      std::pow(1.0 - x[index], 2.0);
        }
        return result;
    };
    return problem;
}

void givens_rotate(double& first, double& second, double angle) {
    const double cosine = std::cos(angle);
    const double sine = std::sin(angle);
    const double old_first = first;
    const double old_second = second;
    first = cosine * old_first - sine * old_second;
    second = sine * old_first + cosine * old_second;
}

Problem make_rotated_rastrigin6() {
    Problem problem;
    problem.name = "rotated_rastrigin6";
    constexpr std::array<double, 6> shift{3.1, -2.7, 1.9, -3.3, 2.4, -1.5};
    for (std::size_t index = 0; index < shift.size(); ++index) {
        const std::string name = "x" + std::to_string(index + 1);
        problem.space.add(RealDimension(name, -5.12, 5.12));
        problem.known_optimum.values.emplace(name, shift[index]);
    }
    problem.default_trials = 640;
    problem.optimum = 0.0;
    problem.objective = [](const Candidate& candidate) {
        constexpr std::array<double, 6> shift{3.1, -2.7, 1.9, -3.3, 2.4, -1.5};
        std::array<double, 6> z{};
        for (std::size_t index = 0; index < z.size(); ++index) {
            z[index] = real_value(candidate, "x" + std::to_string(index + 1)) - shift[index];
        }
        givens_rotate(z[0], z[1], 0.41);
        givens_rotate(z[2], z[5], -0.73);
        givens_rotate(z[1], z[4], 1.07);
        givens_rotate(z[0], z[3], -0.56);
        givens_rotate(z[2], z[4], 0.92);
        givens_rotate(z[1], z[5], -1.19);
        givens_rotate(z[3], z[4], 0.67);
        givens_rotate(z[0], z[2], -0.88);
        givens_rotate(z[3], z[5], 1.31);
        givens_rotate(z[0], z[4], 0.35);
        givens_rotate(z[1], z[3], -0.64);
        givens_rotate(z[2], z[5], 0.79);

        double result = 10.0 * static_cast<double>(z.size());
        for (const double value : z) {
            result += value * value - 10.0 * std::cos(2.0 * kPi * value);
        }
        return result;
    };
    return problem;
}

Problem make_mixed_log_interaction() {
    Problem problem;
    problem.name = "mixed_log";
    problem.space.add(CategoricalDimension(
        "mode", std::vector<ParameterValue>{std::string("trend"), std::string("mean"),
                                            std::string("breakout"), std::string("hybrid"),
                                            std::string("carry"), std::string("noise")}));
    problem.space.add(BooleanDimension("enabled"));
    problem.space.add(IntegerDimension("depth", 1, 96));
    problem.space.add(IntegerDimension("period", 1, 1'000'000, 1, true));
    problem.space.add(RealDimension("rate", 1e-8, 1e2, std::nullopt, true));
    problem.space.add(RealDimension("leverage", 0.2, 3.0, 0.1));
    problem.default_trials = 384;
    problem.optimum = 0.0;
    problem.known_optimum.values = {
        {"mode", std::string("hybrid")}, {"enabled", true}, {"depth", std::int64_t{37}},
        {"period", std::int64_t{3'000}}, {"rate", 0.002},   {"leverage", 1.4},
    };
    problem.objective = [](const Candidate& candidate) {
        struct Basin {
            double depth;
            double period;
            double rate;
            double leverage;
            double offset;
        };
        const std::string& mode = string_value(candidate, "mode");
        Basin basin{};
        if (mode == "trend") {
            basin = {12.0, 80.0, 2e-5, 0.7, 0.55};
        } else if (mode == "mean") {
            basin = {75.0, 120'000.0, 0.8, 2.3, 0.80};
        } else if (mode == "breakout") {
            basin = {24.0, 900.0, 8e-3, 1.9, 0.35};
        } else if (mode == "hybrid") {
            basin = {37.0, 3'000.0, 0.002, 1.4, 0.0};
        } else if (mode == "carry") {
            basin = {88.0, 600'000.0, 2e-7, 0.4, 1.10};
        } else {
            basin = {5.0, 12.0, 20.0, 2.8, 2.50};
        }

        const double depth = static_cast<double>(integer_value(candidate, "depth"));
        const double period = static_cast<double>(integer_value(candidate, "period"));
        const double rate = real_value(candidate, "rate");
        const double leverage = real_value(candidate, "leverage");
        const double d = (depth - basin.depth) / 12.0;
        const double p = std::log(period / basin.period);
        const double r = std::log(rate / basin.rate);
        const double l = (leverage - basin.leverage) / 0.5;
        const double disabled = boolean_value(candidate, "enabled") ? 0.0 : 1.5;
        return basin.offset + disabled + std::pow(d + 0.45 * p - 0.20 * l, 2.0) +
               0.7 * std::pow(p - 0.55 * r, 2.0) + 0.6 * std::pow(r + 0.35 * l, 2.0) +
               0.45 * std::pow(l - 0.25 * d, 2.0) + 0.025 * std::pow(p * r, 2.0) +
               0.04 * (1.0 - std::cos(2.0 * kPi * d));
    };
    return problem;
}

std::int64_t circular_residue(std::int64_t value, std::int64_t modulus) {
    std::int64_t residue = value % modulus;
    if (residue < 0) {
        residue += modulus;
    }
    return std::min(residue, modulus - residue);
}

std::int64_t integer_square(std::int64_t value) {
    return value * value;
}

double million_discrete_objective(const Candidate& candidate) {
    std::array<std::int64_t, 6> d{};
    for (std::size_t index = 0; index < d.size(); ++index) {
        d[index] =
            integer_value(candidate, "x" + std::to_string(index + 1)) - kMillionTarget[index];
    }
    const std::int64_t rugged =
        17 * integer_square(circular_residue(3 * d[0] + 5 * d[1] + 7 * d[2] + 2 * d[3], 11)) +
        13 * integer_square(circular_residue(2 * d[1] + 3 * d[2] + 5 * d[3] + 7 * d[4], 13)) +
        11 * integer_square(circular_residue(5 * d[0] + 2 * d[2] + 3 * d[4] + 7 * d[5], 17)) +
        19 * integer_square(circular_residue(d[0] * d[3] + 2 * d[1] * d[4] + 3 * d[2] * d[5], 11)) +
        7 * integer_square(circular_residue(d[0] * d[1] + d[2] * d[3] + d[4] * d[5], 13));
    std::int64_t anchor = 0;
    for (const std::int64_t value : d) {
        anchor += integer_square(value);
    }
    return static_cast<double>(100 * rugged + anchor);
}

Problem make_million_discrete() {
    Problem problem;
    problem.name = "million_discrete";
    for (std::size_t index = 0; index < kMillionTarget.size(); ++index) {
        const std::string name = "x" + std::to_string(index + 1);
        problem.space.add(IntegerDimension(name, 0, 9));
        problem.known_optimum.values.emplace(name, kMillionTarget[index]);
    }
    problem.default_trials = 1'000;
    problem.optimum = 0.0;
    problem.objective = million_discrete_objective;
    return problem;
}

Problem select_problem(const std::string& name) {
    if (name == "branin2") {
        return make_branin();
    }
    if (name == "hartmann3") {
        return make_hartmann3();
    }
    if (name == "rosenbrock6") {
        return make_rosenbrock6();
    }
    if (name == "rotated_rastrigin6") {
        return make_rotated_rastrigin6();
    }
    if (name == "mixed_log") {
        return make_mixed_log_interaction();
    }
    if (name == "million_discrete") {
        return make_million_discrete();
    }
    throw std::invalid_argument("unknown problem: " + name);
}

std::uint64_t parse_unsigned(const char* text, const std::string& option) {
    std::size_t consumed = 0;
    const std::string value(text);
    if (value.empty() || value.front() == '-') {
        throw std::invalid_argument(option + " expects an unsigned integer");
    }
    const unsigned long long parsed = std::stoull(value, &consumed);
    if (consumed != value.size()) {
        throw std::invalid_argument(option + " expects an unsigned integer");
    }
    return static_cast<std::uint64_t>(parsed);
}

struct Options {
    std::string problem;
    std::uint64_t seed = 0;
    std::uint64_t trials = 0;
    std::uint64_t startup_trials = 10;
};

Options parse_options(int argc, char** argv) {
    Options options;
    for (int index = 1; index < argc; ++index) {
        const std::string argument(argv[index]);
        if (argument == "--problem" && index + 1 < argc) {
            options.problem = argv[++index];
        } else if (argument == "--seed" && index + 1 < argc) {
            options.seed = parse_unsigned(argv[++index], "--seed");
        } else if (argument == "--trials" && index + 1 < argc) {
            options.trials = parse_unsigned(argv[++index], "--trials");
        } else if (argument == "--startup-trials" && index + 1 < argc) {
            options.startup_trials = parse_unsigned(argv[++index], "--startup-trials");
        } else {
            throw std::invalid_argument("unknown or incomplete option: " + argument);
        }
    }
    if (options.problem.empty()) {
        throw std::invalid_argument("--problem is required");
    }
    if (options.startup_trials == 0) {
        throw std::invalid_argument("--startup-trials must be positive");
    }
    return options;
}

std::int64_t nanoseconds(std::chrono::steady_clock::duration duration) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(duration).count();
}

struct MillionDiagnostics {
    std::uint64_t code = 0;
    std::int64_t hamming = 0;
    std::int64_t l1 = 0;
};

MillionDiagnostics million_diagnostics(const Candidate& candidate) {
    MillionDiagnostics result;
    std::uint64_t multiplier = 1;
    for (std::size_t index = 0; index < kMillionTarget.size(); ++index) {
        const std::int64_t value = integer_value(candidate, "x" + std::to_string(index + 1));
        result.code += static_cast<std::uint64_t>(value) * multiplier;
        multiplier *= 10;
        const std::int64_t difference = std::abs(value - kMillionTarget[index]);
        result.hamming += difference != 0 ? 1 : 0;
        result.l1 += difference;
    }
    return result;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const Options options = parse_options(argc, argv);
        const Problem problem = select_problem(options.problem);
        const std::uint64_t trials = options.trials == 0 ? problem.default_trials : options.trials;
        require(trials > 0, "trial budget must be positive");
        require(problem.space.is_valid(problem.known_optimum), "known optimum is invalid");
        require(std::abs(problem.objective(problem.known_optimum) - problem.optimum) <= 2e-5,
                "known optimum and objective formula disagree");

        TpeSamplerConfig config;
        config.startup_trials = options.startup_trials;
        config.ei_candidates = 24;
        config.gamma_fraction = 0.10;
        config.gamma_cap = 25;
        config.prior_weight = 1.0;
        config.constant_liar = false;

        std::chrono::steady_clock::duration sampler_elapsed{};
        std::chrono::steady_clock::duration objective_elapsed{};
        std::chrono::steady_clock::duration validation_elapsed{};
        const bool is_million_discrete = problem.name == "million_discrete";
        std::unordered_set<std::uint64_t> unique_candidates;
        std::uint64_t exact_optimum_hits = 0;
        std::int64_t best_hamming = -1;
        std::int64_t best_l1 = -1;
        TpeSampler sampler(problem.space, options.seed, ObjectiveDirection::Minimize, trials,
                           config);
        // Match the Python path: TPESampler/Study construction is setup and is
        // excluded from both public sampler overhead and optimization wall time.
        const auto wall_start = std::chrono::steady_clock::now();

        double best = std::numeric_limits<double>::infinity();
        for (std::uint64_t trial_index = 0; trial_index < trials; ++trial_index) {
            const auto ask_start = std::chrono::steady_clock::now();
            const auto candidate = sampler.ask();
            sampler_elapsed += std::chrono::steady_clock::now() - ask_start;
            require(candidate.has_value(), "native TPE ended before its budget");

            const auto validation_start = std::chrono::steady_clock::now();
            require(candidate->id == trial_index, "native TPE candidate id drifted");
            require(problem.space.is_valid(*candidate), "native TPE candidate was invalid");
            validation_elapsed += std::chrono::steady_clock::now() - validation_start;

            const auto objective_start = std::chrono::steady_clock::now();
            const double value = problem.objective(*candidate);
            objective_elapsed += std::chrono::steady_clock::now() - objective_start;
            require(std::isfinite(value), "objective returned a non-finite value");
            if (is_million_discrete) {
                const MillionDiagnostics diagnostics = million_diagnostics(*candidate);
                unique_candidates.insert(diagnostics.code);
                if (diagnostics.hamming == 0) {
                    ++exact_optimum_hits;
                }
                if (value < best ||
                    (value == best &&
                     (best_hamming < 0 || diagnostics.hamming < best_hamming ||
                      (diagnostics.hamming == best_hamming && diagnostics.l1 < best_l1)))) {
                    best = value;
                    best_hamming = diagnostics.hamming;
                    best_l1 = diagnostics.l1;
                }
            } else {
                best = std::min(best, value);
            }

            const auto tell_start = std::chrono::steady_clock::now();
            sampler.tell(candidate->id, value);
            sampler_elapsed += std::chrono::steady_clock::now() - tell_start;
        }
        const auto wall_elapsed = std::chrono::steady_clock::now() - wall_start;
        const double best_regret = std::max(0.0, best - problem.optimum);
        const std::int64_t unique_count =
            is_million_discrete ? static_cast<std::int64_t>(unique_candidates.size()) : -1;
        const std::int64_t duplicate_count =
            is_million_discrete ? static_cast<std::int64_t>(trials) - unique_count : -1;

        std::cout << std::setprecision(17)
                  << "{\"implementation\":\"pineforge_native\",\"problem\":\"" << problem.name
                  << "\",\"seed\":" << options.seed << ",\"trials\":" << trials
                  << ",\"startup_trials\":" << options.startup_trials << ",\"best_value\":" << best
                  << ",\"optimum\":" << problem.optimum << ",\"best_regret\":" << best_regret
                  << ",\"sampler_ns\":" << nanoseconds(sampler_elapsed)
                  << ",\"objective_ns\":" << nanoseconds(objective_elapsed)
                  << ",\"validation_ns\":" << nanoseconds(validation_elapsed)
                  << ",\"wall_ns\":" << nanoseconds(wall_elapsed)
                  << ",\"unique_candidates\":" << unique_count
                  << ",\"duplicate_candidates\":" << duplicate_count
                  << ",\"exact_optimum_hits\":" << exact_optimum_hits
                  << ",\"best_hamming\":" << best_hamming << ",\"best_l1\":" << best_l1 << "}\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << "native Optuna comparison benchmark failed: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
