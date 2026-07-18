#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "pineforge/hpo/sampler.hpp"
#include "pineforge/hpo/search_space.hpp"
#include "pineforge/hpo/types.hpp"

namespace {

using pineforge::hpo::BooleanDimension;
using pineforge::hpo::Candidate;
using pineforge::hpo::CategoricalDimension;
using pineforge::hpo::DlibGlobalSampler;
using pineforge::hpo::IntegerDimension;
using pineforge::hpo::ObjectiveDirection;
using pineforge::hpo::ParameterValue;
using pineforge::hpo::RandomSampler;
using pineforge::hpo::RealDimension;
using pineforge::hpo::SearchSpace;
using pineforge::hpo::TpeSampler;

constexpr double kPi = 3.141592653589793238462643383279502884;

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
    std::uint64_t budget = 0;
    double optimum = 0.0;
    double ratio_floor = 0.0;
    std::optional<double> absolute_median_gate;
    std::optional<double> random_ratio_gate;
    Candidate known_optimum;
    std::function<double(const Candidate&)> objective;
};

struct OptimizerResult {
    double best = std::numeric_limits<double>::infinity();
    std::chrono::steady_clock::duration elapsed{};
};

struct ProblemSummary {
    const Problem* problem = nullptr;
    std::vector<double> tpe;
    std::vector<double> random;
    std::vector<double> dlib;
    std::chrono::steady_clock::duration tpe_elapsed{};
    std::chrono::steady_clock::duration random_elapsed{};
    std::chrono::steady_clock::duration dlib_elapsed{};
    std::size_t tpe_wins = 0;
    std::size_t ties = 0;
};

double regret(double value, double optimum) {
    return std::max(0.0, value - optimum);
}

double median(std::vector<double> values) {
    require(!values.empty(), "cannot take the median of an empty sample");
    std::sort(values.begin(), values.end());
    const std::size_t middle = values.size() / 2;
    if (values.size() % 2 != 0) {
        return values[middle];
    }
    return 0.5 * (values[middle - 1] + values[middle]);
}

double percentile75(std::vector<double> values) {
    require(!values.empty(), "cannot take a percentile of an empty sample");
    std::sort(values.begin(), values.end());
    const std::size_t index = (3 * (values.size() - 1) + 3) / 4;
    return values[index];
}

Problem make_hartmann6() {
    // Canonical Hartmann-6 coefficients, formula, and [0, 1]^6 domain:
    // https://www.sfu.ca/~ssurjano/hart6.html
    Problem problem;
    problem.name = "Hartmann-6";
    for (std::size_t index = 0; index < 6; ++index) {
        problem.space.add(RealDimension("x" + std::to_string(index + 1), 0.0, 1.0));
    }
    problem.budget = 384;
    problem.optimum = -3.322368011415515;
    problem.ratio_floor = 0.01;
    problem.absolute_median_gate = 0.20;
    problem.known_optimum.values = {
        {"x1", 0.20169},  {"x2", 0.150011}, {"x3", 0.476874},
        {"x4", 0.275332}, {"x5", 0.311652}, {"x6", 0.6573},
    };
    problem.objective = [](const Candidate& candidate) {
        constexpr std::array<double, 4> alpha{1.0, 1.2, 3.0, 3.2};
        constexpr std::array<std::array<double, 6>, 4> a{{
            {{10.0, 3.0, 17.0, 3.5, 1.7, 8.0}},
            {{0.05, 10.0, 17.0, 0.1, 8.0, 14.0}},
            {{3.0, 3.5, 1.7, 10.0, 17.0, 8.0}},
            {{17.0, 8.0, 0.05, 10.0, 0.1, 14.0}},
        }};
        constexpr std::array<std::array<double, 6>, 4> p{{
            {{0.1312, 0.1696, 0.5569, 0.0124, 0.8283, 0.5886}},
            {{0.2329, 0.4135, 0.8307, 0.3736, 0.1004, 0.9991}},
            {{0.2348, 0.1451, 0.3522, 0.2883, 0.3047, 0.6650}},
            {{0.4047, 0.8828, 0.8732, 0.5743, 0.1091, 0.0381}},
        }};
        const std::array<double, 6> x{
            real_value(candidate, "x1"), real_value(candidate, "x2"), real_value(candidate, "x3"),
            real_value(candidate, "x4"), real_value(candidate, "x5"), real_value(candidate, "x6"),
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
    // Canonical Rosenbrock chain, extended to six dimensions:
    // https://www.sfu.ca/~ssurjano/rosen.html
    Problem problem;
    problem.name = "Rosenbrock-6";
    for (std::size_t index = 0; index < 6; ++index) {
        problem.space.add(RealDimension("x" + std::to_string(index + 1), -2.048, 2.048));
        problem.known_optimum.values.emplace("x" + std::to_string(index + 1), 1.0);
    }
    problem.budget = 512;
    problem.optimum = 0.0;
    problem.ratio_floor = 1.0;
    problem.random_ratio_gate = 0.55;
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
    // The base Rastrigin definition is canonical:
    // https://www.sfu.ca/~ssurjano/rastr.html
    // A fixed composition of Givens rotations supplies an exactly orthogonal
    // matrix Q.  Evaluating Rastrigin(Q(x-s)) at a fixed off-centre shift s
    // preserves an auditable optimum while breaking the axis-aligned
    // separability assumed by this product-of-marginals TPE.  The shift also
    // prevents the numeric prior at the domain midpoint from getting a free
    // hit at the optimum.
    Problem problem;
    problem.name = "Rot-Rastrigin-6";
    for (std::size_t index = 0; index < 6; ++index) {
        problem.space.add(RealDimension("x" + std::to_string(index + 1), -5.12, 5.12));
    }
    constexpr std::array<double, 6> shift{3.1, -2.7, 1.9, -3.3, 2.4, -1.5};
    for (std::size_t index = 0; index < shift.size(); ++index) {
        problem.known_optimum.values.emplace("x" + std::to_string(index + 1), shift[index]);
    }
    problem.budget = 640;
    problem.optimum = 0.0;
    problem.ratio_floor = 1.0;
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
    // A self-designed PineForge-shaped problem.  Categories select distinct
    // nonlinear basins, while log-scale integer/real variables, an ordinary
    // integer, a stepped real, and a Boolean interact.  Every term is
    // non-negative, so the exact global optimum is independently auditable.
    Problem problem;
    problem.name = "Mixed-log-interact";
    problem.space.add(CategoricalDimension(
        "mode", std::vector<ParameterValue>{std::string("trend"), std::string("mean"),
                                            std::string("breakout"), std::string("hybrid"),
                                            std::string("carry"), std::string("noise")}));
    problem.space.add(BooleanDimension("enabled"));
    problem.space.add(IntegerDimension("depth", 1, 96));
    problem.space.add(IntegerDimension("period", 1, 1'000'000, 1, true));
    problem.space.add(RealDimension("rate", 1e-8, 1e2, std::nullopt, true));
    problem.space.add(RealDimension("leverage", 0.2, 3.0, 0.1));
    problem.budget = 384;
    problem.optimum = 0.0;
    problem.ratio_floor = 0.02;
    problem.absolute_median_gate = 0.50;
    problem.random_ratio_gate = 0.50;
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

OptimizerResult run_tpe(const Problem& problem, std::uint64_t seed) {
    const auto start = std::chrono::steady_clock::now();
    TpeSampler sampler(problem.space, seed, ObjectiveDirection::Minimize, problem.budget);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t trial = 0; trial < problem.budget; ++trial) {
        const auto candidate = sampler.ask();
        require(candidate.has_value(), problem.name + ": TPE ended before its budget");
        require(candidate->id == trial, problem.name + ": TPE candidate id drifted");
        require(problem.space.is_valid(*candidate), problem.name + ": TPE candidate invalid");
        const double value = problem.objective(*candidate);
        require(std::isfinite(value), problem.name + ": TPE objective was not finite");
        best = std::min(best, value);
        sampler.tell(candidate->id, value);
    }
    require(!sampler.ask().has_value(), problem.name + ": TPE exceeded max_candidates");
    require(sampler.completed() == problem.budget, problem.name + ": TPE completion count drifted");
    require(sampler.outstanding() == 0, problem.name + ": TPE leaked an outstanding trial");
    return {best, std::chrono::steady_clock::now() - start};
}

OptimizerResult run_random(const Problem& problem, std::uint64_t seed) {
    const auto start = std::chrono::steady_clock::now();
    RandomSampler sampler(problem.space, seed, problem.budget);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t trial = 0; trial < problem.budget; ++trial) {
        const auto candidate = sampler.next();
        require(candidate.has_value(), problem.name + ": random ended before its budget");
        require(candidate->id == trial, problem.name + ": random candidate id drifted");
        require(problem.space.is_valid(*candidate), problem.name + ": random candidate invalid");
        const double value = problem.objective(*candidate);
        require(std::isfinite(value), problem.name + ": random objective was not finite");
        best = std::min(best, value);
    }
    require(!sampler.next().has_value(), problem.name + ": random exceeded max_candidates");
    return {best, std::chrono::steady_clock::now() - start};
}

OptimizerResult run_dlib(const Problem& problem, std::uint64_t seed) {
    const auto start = std::chrono::steady_clock::now();
    DlibGlobalSampler sampler(problem.space, seed, ObjectiveDirection::Minimize, problem.budget);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t trial = 0; trial < problem.budget; ++trial) {
        const auto candidate = sampler.ask();
        require(candidate.has_value(), problem.name + ": dlib ended before its budget");
        require(candidate->id == trial, problem.name + ": dlib candidate id drifted");
        require(problem.space.is_valid(*candidate), problem.name + ": dlib candidate invalid");
        const double value = problem.objective(*candidate);
        require(std::isfinite(value), problem.name + ": dlib objective was not finite");
        best = std::min(best, value);
        sampler.tell(candidate->id, value);
    }
    require(!sampler.ask().has_value(), problem.name + ": dlib exceeded max_candidates");
    require(sampler.completed() == problem.budget,
            problem.name + ": dlib completion count drifted");
    require(sampler.outstanding() == 0, problem.name + ": dlib leaked an outstanding trial");
    return {best, std::chrono::steady_clock::now() - start};
}

ProblemSummary evaluate(const Problem& problem, const std::vector<std::uint64_t>& seeds) {
    ProblemSummary summary;
    summary.problem = &problem;
    for (const std::uint64_t seed : seeds) {
        const OptimizerResult tpe = run_tpe(problem, seed);
        const OptimizerResult random = run_random(problem, seed);
        const OptimizerResult dlib = run_dlib(problem, seed);
        const double tpe_regret = regret(tpe.best, problem.optimum);
        const double random_regret = regret(random.best, problem.optimum);
        const double dlib_regret = regret(dlib.best, problem.optimum);
        summary.tpe.push_back(tpe_regret);
        summary.random.push_back(random_regret);
        summary.dlib.push_back(dlib_regret);
        summary.tpe_elapsed += tpe.elapsed;
        summary.random_elapsed += random.elapsed;
        summary.dlib_elapsed += dlib.elapsed;

        const double tolerance =
            1e-12 * std::max({1.0, std::abs(tpe_regret), std::abs(random_regret)});
        if (tpe_regret + tolerance < random_regret) {
            ++summary.tpe_wins;
        } else if (std::abs(tpe_regret - random_regret) <= tolerance) {
            ++summary.ties;
        }
    }
    return summary;
}

void verify_replay(const Problem& problem, std::uint64_t seed, std::uint64_t trials) {
    TpeSampler first(problem.space, seed, ObjectiveDirection::Minimize, trials);
    TpeSampler second(problem.space, seed, ObjectiveDirection::Minimize, trials);
    for (std::uint64_t trial = 0; trial < trials; ++trial) {
        const auto left = first.ask();
        const auto right = second.ask();
        require(left.has_value() && right.has_value(), "TPE replay ended early");
        require(left->id == right->id, "TPE replay candidate ids differ");
        require(left->values == right->values, "TPE replay candidate values differ");
        require(problem.space.is_valid(*left), "TPE replay produced an invalid candidate");
        const double value = problem.objective(*left);
        first.tell(left->id, value);
        second.tell(right->id, value);
    }
}

double milliseconds(std::chrono::steady_clock::duration duration) {
    return std::chrono::duration<double, std::milli>(duration).count();
}

void print_results(const std::vector<ProblemSummary>& summaries,
                   const std::vector<std::uint64_t>& seeds) {
    std::cout << "\nHard native TPE stress benchmark (minimize, equal budgets)\n";
    std::cout << std::left << std::setw(20) << "problem" << std::right << std::setw(7) << "seed"
              << std::setw(14) << "TPE regret" << std::setw(14) << "random" << std::setw(14)
              << "dlib" << std::setw(11) << "TPE/random" << '\n';
    for (const auto& summary : summaries) {
        for (std::size_t index = 0; index < seeds.size(); ++index) {
            const double ratio = (summary.tpe[index] + summary.problem->ratio_floor) /
                                 (summary.random[index] + summary.problem->ratio_floor);
            std::cout << std::left << std::setw(20) << summary.problem->name << std::right
                      << std::setw(7) << seeds[index] << std::scientific << std::setprecision(4)
                      << std::setw(14) << summary.tpe[index] << std::setw(14)
                      << summary.random[index] << std::setw(14) << summary.dlib[index] << std::fixed
                      << std::setprecision(3) << std::setw(11) << ratio << '\n';
        }
    }

    std::cout << "\n"
              << std::left << std::setw(20) << "problem" << std::right << std::setw(7) << "budget"
              << std::setw(14) << "TPE median" << std::setw(14) << "TPE p75" << std::setw(14)
              << "rnd median" << std::setw(14) << "dlib median" << std::setw(9) << "W/T/L"
              << std::setw(13) << "TPE ms" << std::setw(13) << "rnd ms" << std::setw(13)
              << "dlib ms" << '\n';
    for (const auto& summary : summaries) {
        const std::size_t losses = seeds.size() - summary.tpe_wins - summary.ties;
        const std::string record = std::to_string(summary.tpe_wins) + "/" +
                                   std::to_string(summary.ties) + "/" + std::to_string(losses);
        std::cout << std::left << std::setw(20) << summary.problem->name << std::right
                  << std::setw(7) << summary.problem->budget << std::scientific
                  << std::setprecision(4) << std::setw(14) << median(summary.tpe) << std::setw(14)
                  << percentile75(summary.tpe) << std::setw(14) << median(summary.random)
                  << std::setw(14) << median(summary.dlib) << std::defaultfloat << std::setw(9)
                  << record << std::fixed << std::setprecision(1) << std::setw(13)
                  << milliseconds(summary.tpe_elapsed) << std::setw(13)
                  << milliseconds(summary.random_elapsed) << std::setw(13)
                  << milliseconds(summary.dlib_elapsed) << '\n';
    }
}

}  // namespace

int main() {
    try {
        const auto suite_start = std::chrono::steady_clock::now();
        const std::vector<Problem> problems{make_hartmann6(), make_rosenbrock6(),
                                            make_rotated_rastrigin6(),
                                            make_mixed_log_interaction()};
        const std::vector<std::uint64_t> seeds{17, 41, 73, 109, 149};

        for (const auto& problem : problems) {
            require(problem.space.is_valid(problem.known_optimum),
                    problem.name + ": declared optimum is outside the search space");
            const double known_value = problem.objective(problem.known_optimum);
            const double tolerance = 2e-5 * std::max(1.0, std::abs(problem.optimum));
            require(std::abs(known_value - problem.optimum) <= tolerance,
                    problem.name + ": declared optimum does not match its formula");
        }

        // Exact fixed-seed replay is a hard API contract.  Use the hardest
        // mixed space so this covers every supported dimension family.
        verify_replay(problems.back(), 0x5eedU, 128);

        std::vector<ProblemSummary> summaries;
        for (const auto& problem : problems) {
            summaries.push_back(evaluate(problem, seeds));
        }
        print_results(summaries, seeds);

        std::size_t median_wins = 0;
        std::size_t paired_wins = 0;
        std::size_t paired_ties = 0;
        double log_ratio_sum = 0.0;
        std::size_t ratio_count = 0;
        for (const auto& summary : summaries) {
            const double tpe_median = median(summary.tpe);
            const double random_median = median(summary.random);
            if (tpe_median < random_median) {
                ++median_wins;
            }
            paired_wins += summary.tpe_wins;
            paired_ties += summary.ties;
            for (std::size_t index = 0; index < seeds.size(); ++index) {
                const double ratio = (summary.tpe[index] + summary.problem->ratio_floor) /
                                     (summary.random[index] + summary.problem->ratio_floor);
                log_ratio_sum += std::clamp(std::log(ratio), -std::log(100.0), std::log(100.0));
                ++ratio_count;
            }
        }

        const double geometric_paired_ratio =
            std::exp(log_ratio_sum / static_cast<double>(ratio_count));
        const double paired_win_rate =
            static_cast<double>(paired_wins) / static_cast<double>(ratio_count);
        const auto suite_elapsed = std::chrono::steady_clock::now() - suite_start;
        std::cout << std::defaultfloat << "\naggregate: median wins " << median_wins << '/'
                  << problems.size() << ", paired " << paired_wins << '/' << paired_ties << '/'
                  << ratio_count - paired_wins - paired_ties << ", win rate " << std::fixed
                  << std::setprecision(1) << 100.0 * paired_win_rate
                  << "%, geometric paired regret ratio " << std::setprecision(3)
                  << geometric_paired_ratio << ", wall " << std::setprecision(1)
                  << milliseconds(suite_elapsed) << " ms\n";

        // Hard aggregate gates: product TPE need not win every non-separable
        // class, but it must provide material value over equal-budget random
        // search across this deliberately hostile suite.
        require(median_wins >= 3, "TPE did not beat random on enough problem medians");
        require(paired_win_rate >= 0.55, "TPE paired win rate fell below 55%");
        require(geometric_paired_ratio <= 0.75,
                "TPE aggregate geometric regret ratio exceeded 0.75");

        // Per-problem gates are calibrated after the public definitions, not
        // against dlib.  Rotated Rastrigin is intentionally diagnostic: a
        // product TPE has no covariance model, so its loss is printed and
        // counted in every aggregate gate rather than concealed by an easy
        // problem-specific threshold.
        for (const auto& summary : summaries) {
            const double tpe_median = median(summary.tpe);
            const double random_median = median(summary.random);
            if (summary.problem->absolute_median_gate.has_value()) {
                require(tpe_median <= *summary.problem->absolute_median_gate,
                        summary.problem->name + ": TPE median regret exceeded absolute gate");
            }
            if (summary.problem->random_ratio_gate.has_value()) {
                require(tpe_median <= *summary.problem->random_ratio_gate * random_median,
                        summary.problem->name + ": TPE/random median ratio exceeded gate");
            }
        }

        std::cout << "hard native TPE stress checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "hard native TPE stress failure: " << error.what() << '\n';
        return 1;
    }
}
