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

int failures = 0;

#define CHECK(condition)                                                                       \
    do {                                                                                       \
        if (!(condition)) {                                                                    \
            std::cerr << __FILE__ << ':' << __LINE__ << ": check failed: " #condition << '\n'; \
            ++failures;                                                                        \
        }                                                                                      \
    } while (false)

struct Problem {
    std::string name;
    SearchSpace space;
    std::uint64_t budget = 0;
    double optimum = 0.0;
    double median_target = 0.0;
    Candidate known_optimum;
    std::function<double(const Candidate&)> objective;
};

struct Summary {
    const Problem* problem = nullptr;
    std::vector<double> tpe_regrets;
    std::vector<double> random_regrets;
    std::vector<double> dlib_regrets;
    double tpe_median = 0.0;
    double random_median = 0.0;
    double dlib_median = 0.0;
    double tpe_microseconds_per_proposal = 0.0;
    std::size_t tpe_wins = 0;
    std::size_t ties = 0;
};

double real_value(const Candidate& candidate, const std::string& name) {
    return std::get<double>(*candidate.find(name));
}

std::int64_t integer_value(const Candidate& candidate, const std::string& name) {
    return std::get<std::int64_t>(*candidate.find(name));
}

bool boolean_value(const Candidate& candidate, const std::string& name) {
    return std::get<bool>(*candidate.find(name));
}

const std::string& string_value(const Candidate& candidate, const std::string& name) {
    return std::get<std::string>(*candidate.find(name));
}

double median(std::vector<double> values) {
    CHECK(!values.empty());
    std::sort(values.begin(), values.end());
    const std::size_t middle = values.size() / 2;
    if (values.size() % 2 != 0) {
        return values[middle];
    }
    return 0.5 * (values[middle - 1] + values[middle]);
}

double regret(double value, double optimum) {
    // Tiny negative values can arise from the rounded published Hartmann
    // optimum.  A regret is non-negative by definition.
    return std::max(0.0, value - optimum);
}

Problem make_branin() {
    // Canonical Branin-Hoo definition and domain:
    // https://www.sfu.ca/~ssurjano/branin.html
    Problem problem;
    problem.name = "Branin-2";
    problem.space.add(RealDimension("x1", -5.0, 10.0));
    problem.space.add(RealDimension("x2", 0.0, 15.0));
    problem.budget = 128;
    problem.optimum = 0.39788735772973816;
    problem.median_target = 0.10;
    problem.known_optimum.values = {{"x1", -kPi}, {"x2", 12.275}};
    problem.objective = [](const Candidate& candidate) {
        const double x1 = real_value(candidate, "x1");
        const double x2 = real_value(candidate, "x2");
        constexpr double a = 1.0;
        const double b = 5.1 / (4.0 * kPi * kPi);
        const double c = 5.0 / kPi;
        constexpr double r = 6.0;
        constexpr double s = 10.0;
        const double t = 1.0 / (8.0 * kPi);
        return a * std::pow(x2 - b * x1 * x1 + c * x1 - r, 2.0) + s * (1.0 - t) * std::cos(x1) + s;
    };
    return problem;
}

Problem make_hartmann3() {
    // Canonical 3-D Hartmann definition and domain:
    // https://www.sfu.ca/~ssurjano/hart3.html
    Problem problem;
    problem.name = "Hartmann-3";
    problem.space.add(RealDimension("x1", 0.0, 1.0));
    problem.space.add(RealDimension("x2", 0.0, 1.0));
    problem.space.add(RealDimension("x3", 0.0, 1.0));
    problem.budget = 192;
    // This value follows the rounded coefficient table above; published
    // summaries commonly round it to -3.86278.
    problem.optimum = -3.8627797869493365;
    problem.median_target = 0.01;
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
        const std::array<double, 3> x{real_value(candidate, "x1"), real_value(candidate, "x2"),
                                      real_value(candidate, "x3")};
        double value = 0.0;
        for (std::size_t i = 0; i < alpha.size(); ++i) {
            double exponent = 0.0;
            for (std::size_t j = 0; j < x.size(); ++j) {
                exponent += a[i][j] * std::pow(x[j] - p[i][j], 2.0);
            }
            value -= alpha[i] * std::exp(-exponent);
        }
        return value;
    };
    return problem;
}

Problem make_rastrigin() {
    // Canonical Rastrigin definition and domain:
    // https://www.sfu.ca/~ssurjano/rastr.html
    Problem problem;
    problem.name = "Rastrigin-2";
    problem.space.add(RealDimension("x1", -5.12, 5.12));
    problem.space.add(RealDimension("x2", -5.12, 5.12));
    problem.budget = 384;
    problem.optimum = 0.0;
    // At a coordinate-one local basin the objective is 1.0, so this gate
    // requires a median result at least as good as that first neighboring
    // basin without pretending every run must land at the exact origin.
    problem.median_target = 1.0;
    problem.known_optimum.values = {{"x1", 0.0}, {"x2", 0.0}};
    problem.objective = [](const Candidate& candidate) {
        const double x1 = real_value(candidate, "x1");
        const double x2 = real_value(candidate, "x2");
        return 20.0 + x1 * x1 - 10.0 * std::cos(2.0 * kPi * x1) + x2 * x2 -
               10.0 * std::cos(2.0 * kPi * x2);
    };
    return problem;
}

Problem make_rosenbrock() {
    // Canonical Rosenbrock definition and restricted domain:
    // https://www.sfu.ca/~ssurjano/rosen.html
    Problem problem;
    problem.name = "Rosenbrock-2";
    problem.space.add(RealDimension("x1", -2.048, 2.048));
    problem.space.add(RealDimension("x2", -2.048, 2.048));
    problem.budget = 320;
    problem.optimum = 0.0;
    problem.median_target = 0.15;
    problem.known_optimum.values = {{"x1", 1.0}, {"x2", 1.0}};
    problem.objective = [](const Candidate& candidate) {
        const double x1 = real_value(candidate, "x1");
        const double x2 = real_value(candidate, "x2");
        return 100.0 * std::pow(x2 - x1 * x1, 2.0) + std::pow(1.0 - x1, 2.0);
    };
    return problem;
}

Problem make_mixed_interaction() {
    // A PineForge-shaped mixed search space.  Each categorical mode owns a
    // nonlinear basin with different numeric optima; only the hybrid/enabled
    // basin has zero offset.  The cross terms make the numeric response
    // non-separable while retaining an exactly known global optimum.
    Problem problem;
    problem.name = "Mixed-interact";
    problem.space.add(IntegerDimension("lookback", 2, 30, 2));
    problem.space.add(RealDimension("risk", 0.1, 2.0, 0.1));
    problem.space.add(RealDimension("threshold", -3.0, 3.0));
    problem.space.add(BooleanDimension("enabled"));
    problem.space.add(CategoricalDimension(
        "mode", std::vector<ParameterValue>{std::string("trend"), std::string("mean"),
                                            std::string("hybrid"), std::string("noise")}));
    problem.budget = 160;
    problem.optimum = 0.0;
    problem.median_target = 0.05;
    problem.known_optimum.values = {{"lookback", std::int64_t{18}},
                                    {"risk", 1.3},
                                    {"threshold", 0.6},
                                    {"enabled", true},
                                    {"mode", std::string("hybrid")}};
    problem.objective = [](const Candidate& candidate) {
        struct Basin {
            double lookback;
            double risk;
            double threshold;
            double offset;
        };

        const std::string& mode = string_value(candidate, "mode");
        Basin basin{};
        if (mode == "trend") {
            basin = {8.0, 0.5, 1.8, 0.9};
        } else if (mode == "mean") {
            basin = {26.0, 1.8, -1.5, 1.2};
        } else if (mode == "hybrid") {
            basin = {18.0, 1.3, 0.6, 0.0};
        } else {
            basin = {4.0, 1.0, -2.4, 4.0};
        }

        const double u =
            (static_cast<double>(integer_value(candidate, "lookback")) - basin.lookback) / 4.0;
        const double v = (real_value(candidate, "risk") - basin.risk) / 0.3;
        const double w = real_value(candidate, "threshold") - basin.threshold;
        const double disabled_penalty = boolean_value(candidate, "enabled") ? 0.0 : 2.0;
        return basin.offset + disabled_penalty + std::pow(u + 0.35 * v, 2.0) +
               0.55 * std::pow(v - 0.25 * w, 2.0) + 0.4 * w * w + 0.08 * std::pow(u * v, 2.0);
    };
    return problem;
}

double run_tpe(const Problem& problem, std::uint64_t seed) {
    TpeSampler sampler(problem.space, seed, ObjectiveDirection::Minimize, problem.budget);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t trial = 0; trial < problem.budget; ++trial) {
        const auto candidate = sampler.ask();
        CHECK(candidate.has_value());
        if (!candidate.has_value()) {
            break;
        }
        CHECK(candidate->id == trial);
        CHECK(problem.space.is_valid(*candidate));
        const double value = problem.objective(*candidate);
        CHECK(std::isfinite(value));
        best = std::min(best, value);
        sampler.tell(candidate->id, value);
    }
    CHECK(sampler.generated() == problem.budget);
    CHECK(sampler.completed() == problem.budget);
    CHECK(sampler.outstanding() == 0);
    return best;
}

double run_random(const Problem& problem, std::uint64_t seed) {
    RandomSampler sampler(problem.space, seed, problem.budget);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t trial = 0; trial < problem.budget; ++trial) {
        const auto candidate = sampler.next();
        CHECK(candidate.has_value());
        if (!candidate.has_value()) {
            break;
        }
        CHECK(candidate->id == trial);
        CHECK(problem.space.is_valid(*candidate));
        const double value = problem.objective(*candidate);
        CHECK(std::isfinite(value));
        best = std::min(best, value);
    }
    CHECK(sampler.generated() == problem.budget);
    return best;
}

double run_dlib(const Problem& problem, std::uint64_t seed) {
    DlibGlobalSampler sampler(problem.space, seed, ObjectiveDirection::Minimize, problem.budget);
    double best = std::numeric_limits<double>::infinity();
    for (std::uint64_t trial = 0; trial < problem.budget; ++trial) {
        const auto candidate = sampler.ask();
        CHECK(candidate.has_value());
        if (!candidate.has_value()) {
            break;
        }
        CHECK(candidate->id == trial);
        CHECK(problem.space.is_valid(*candidate));
        const double value = problem.objective(*candidate);
        CHECK(std::isfinite(value));
        best = std::min(best, value);
        sampler.tell(candidate->id, value);
    }
    CHECK(sampler.generated() == problem.budget);
    CHECK(sampler.completed() == problem.budget);
    CHECK(sampler.outstanding() == 0);
    return best;
}

Summary evaluate(const Problem& problem, const std::vector<std::uint64_t>& seeds) {
    Summary summary;
    summary.problem = &problem;
    std::chrono::steady_clock::duration tpe_elapsed{};
    for (const std::uint64_t seed : seeds) {
        const auto tpe_start = std::chrono::steady_clock::now();
        const double tpe = regret(run_tpe(problem, seed), problem.optimum);
        tpe_elapsed += std::chrono::steady_clock::now() - tpe_start;
        const double random = regret(run_random(problem, seed), problem.optimum);
        const double dlib = regret(run_dlib(problem, seed), problem.optimum);
        summary.tpe_regrets.push_back(tpe);
        summary.random_regrets.push_back(random);
        summary.dlib_regrets.push_back(dlib);

        const double comparison_tolerance =
            1e-12 * std::max({1.0, std::abs(tpe), std::abs(random)});
        if (tpe + comparison_tolerance < random) {
            ++summary.tpe_wins;
        } else if (random + comparison_tolerance >= tpe) {
            ++summary.ties;
        }
    }
    summary.tpe_median = median(summary.tpe_regrets);
    summary.random_median = median(summary.random_regrets);
    summary.dlib_median = median(summary.dlib_regrets);
    const double tpe_microseconds = std::chrono::duration<double, std::micro>(tpe_elapsed).count();
    summary.tpe_microseconds_per_proposal =
        tpe_microseconds /
        static_cast<double>(problem.budget * static_cast<std::uint64_t>(seeds.size()));
    return summary;
}

void print_results(const std::vector<Summary>& summaries,
                   std::size_t total_wins,
                   std::size_t total_ties) {
    const std::size_t seed_count = summaries.empty() ? 0 : summaries.front().tpe_regrets.size();
    std::cout << "\nNative TPE nonlinear quality benchmark (" << seed_count
              << " fixed seeds, minimize, equal budgets)\n";
    std::cout << std::left << std::setw(16) << "problem" << std::right << std::setw(8) << "budget"
              << std::setw(14) << "TPE median" << std::setw(14) << "rnd median" << std::setw(14)
              << "dlib median" << std::setw(10) << "rnd/TPE" << std::setw(10) << "W/T/L"
              << std::setw(12) << "TPE us/ask" << std::setw(12) << "abs gate" << '\n';
    for (const auto& summary : summaries) {
        const std::size_t losses = summary.tpe_regrets.size() - summary.tpe_wins - summary.ties;
        const double ratio = summary.random_median /
                             std::max(summary.tpe_median, std::numeric_limits<double>::epsilon());
        const std::string record = std::to_string(summary.tpe_wins) + "/" +
                                   std::to_string(summary.ties) + "/" + std::to_string(losses);
        std::cout << std::left << std::setw(16) << summary.problem->name << std::right
                  << std::setw(8) << summary.problem->budget << std::scientific
                  << std::setprecision(3) << std::setw(14) << summary.tpe_median << std::setw(14)
                  << summary.random_median << std::setw(14) << summary.dlib_median << std::fixed
                  << std::setprecision(2) << std::setw(10) << ratio << std::setw(10) << record
                  << std::setw(12) << summary.tpe_microseconds_per_proposal << std::scientific
                  << std::setprecision(2) << std::setw(12) << summary.problem->median_target
                  << '\n';
    }
    const std::size_t total_runs =
        std::accumulate(summaries.begin(), summaries.end(), std::size_t{0},
                        [](std::size_t count, const Summary& summary) {
                            return count + summary.tpe_regrets.size();
                        });
    std::cout << std::defaultfloat << "aggregate paired record: " << total_wins << " wins, "
              << total_ties << " ties, " << total_runs - total_wins - total_ties << " losses\n";
}

}  // namespace

int main() {
    const std::vector<Problem> problems{make_branin(), make_hartmann3(), make_rastrigin(),
                                        make_rosenbrock(), make_mixed_interaction()};
    const std::vector<std::uint64_t> seeds{7, 19, 43, 71, 101, 137, 181, 233, 293};

    for (const auto& problem : problems) {
        CHECK(problem.space.is_valid(problem.known_optimum));
        CHECK(std::abs(problem.objective(problem.known_optimum) - problem.optimum) < 2e-7);
    }

    std::vector<Summary> summaries;
    std::size_t median_wins = 0;
    std::size_t total_wins = 0;
    std::size_t total_ties = 0;
    double bounded_log_ratio_sum = 0.0;
    for (const auto& problem : problems) {
        summaries.push_back(evaluate(problem, seeds));
        const Summary& summary = summaries.back();
        CHECK(summary.tpe_median <= problem.median_target);
        if (summary.tpe_median < summary.random_median) {
            ++median_wins;
        }
        total_wins += summary.tpe_wins;
        total_ties += summary.ties;

        // Bound each problem's contribution so a near-zero result on one easy
        // function cannot hide regressions elsewhere.
        const double ratio = (summary.tpe_median + 1e-6 * problem.median_target) /
                             (summary.random_median + 1e-6 * problem.median_target);
        bounded_log_ratio_sum += std::clamp(std::log(ratio), -std::log(100.0), std::log(100.0));
    }

    print_results(summaries, total_wins, total_ties);

    const std::size_t total_runs = problems.size() * seeds.size();
    const double win_rate = static_cast<double>(total_wins) / static_cast<double>(total_runs);
    const double geometric_median_ratio =
        std::exp(bounded_log_ratio_sum / static_cast<double>(problems.size()));
    std::cout << "median wins: " << median_wins << '/' << problems.size()
              << ", paired win rate: " << std::fixed << std::setprecision(1) << 100.0 * win_rate
              << "%, geometric median regret ratio: " << std::setprecision(3)
              << geometric_median_ratio << "\n\n";

    // This is an aggregate quality contract, not an assertion that an
    // adaptive stochastic optimizer wins every problem or every seed.
    CHECK(median_wins >= 4);
    CHECK(win_rate >= 0.60);
    CHECK(geometric_median_ratio <= 0.65);

    if (failures != 0) {
        std::cerr << failures << " TPE quality check(s) failed\n";
        return 1;
    }
    std::cout << "native TPE nonlinear quality checks passed\n";
    return 0;
}
