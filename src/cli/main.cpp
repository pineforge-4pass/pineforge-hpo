#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/objective.hpp>
#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>
#include <pineforge/hpo/trial_executor.hpp>
#include <pineforge/hpo/types.hpp>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_set>
#include <utility>
#include <vector>

namespace pfh = pineforge::hpo;

#ifndef PINEFORGE_HPO_VERSION
#define PINEFORGE_HPO_VERSION "unknown"
#endif

namespace {

enum class Direction { kMaximize, kMinimize };

struct Options {
    std::filesystem::path strategy;
    std::filesystem::path ohlcv;
    std::filesystem::path output;
    std::string objective;
    std::vector<std::string> constraints;
    std::vector<pfh::Dimension> dimensions;
    std::map<std::string, std::vector<pfh::ParameterValue>> categorical_choices;
    std::vector<std::string> categorical_order;
    pfh::ParameterValues fixed_inputs;
    pfh::ParameterValues strategy_overrides;
    std::string sampler = "grid";
    pfh::CandidatePolicy candidate_policy = pfh::CandidatePolicy::SamplerDefault;
    std::string artifact_key;
    std::string input_timeframe;
    std::string script_timeframe;
    std::string chart_timezone;
    std::uint64_t seed = 0;
    std::uint64_t max_trials = 0;
    unsigned workers = 1;
    bool bar_magnifier = false;
    int magnifier_samples = 4;
    pf_magnifier_distribution_t magnifier_distribution = PF_MAGNIFIER_ENDPOINTS;
    Direction direction = Direction::kMaximize;
    pfh::EvaluationPolicy evaluation_policy;
    pfh::TpeSamplerConfig tpe_config;
};

struct TrialRecord {
    std::uint64_t trial_id = 0;
    pfh::Candidate candidate;
    std::string status = "pending";
    std::string error;
    bool feasible = false;
    std::optional<double> objective;
    int total_trades = 0;
    double net_profit = std::numeric_limits<double>::quiet_NaN();
    std::int64_t input_bars_processed = 0;
    std::int64_t script_bars_processed = 0;
    std::int32_t input_tf_seconds = 0;
    std::int32_t script_tf_seconds = 0;
    std::int32_t script_tf_ratio = 0;
    bool needs_aggregation = false;
    std::map<std::string, double> metrics;
};

[[noreturn]] void usage_error(const std::string& message) {
    throw std::invalid_argument(message + "\nRun `pineforge-hpo-native --help` for usage.");
}

std::string require_value(int argc, char** argv, int& index, const std::string& option) {
    if (index + 1 >= argc)
        usage_error(option + " requires a value");
    return argv[++index];
}

std::int64_t parse_i64(const std::string& text, const std::string& where) {
    std::size_t consumed = 0;
    try {
        const auto value = std::stoll(text, &consumed);
        if (consumed != text.size())
            throw std::invalid_argument("trailing characters");
        return value;
    } catch (const std::exception&) {
        usage_error(where + " requires an integer, got `" + text + "`");
    }
}

std::uint64_t parse_u64(const std::string& text, const std::string& where) {
    if (!text.empty() && text.front() == '-') {
        usage_error(where + " requires a non-negative integer, got `" + text + "`");
    }
    std::size_t consumed = 0;
    try {
        const auto value = std::stoull(text, &consumed);
        if (consumed != text.size())
            throw std::invalid_argument("trailing characters");
        return value;
    } catch (const std::exception&) {
        usage_error(where + " requires a non-negative integer, got `" + text + "`");
    }
}

double parse_double(const std::string& text, const std::string& where) {
    std::size_t consumed = 0;
    try {
        const double value = std::stod(text, &consumed);
        if (consumed != text.size() || !std::isfinite(value)) {
            throw std::invalid_argument("invalid finite number");
        }
        return value;
    } catch (const std::exception&) {
        usage_error(where + " requires a finite number, got `" + text + "`");
    }
}

bool parse_bool(const std::string& text, const std::string& where) {
    if (text == "true" || text == "1")
        return true;
    if (text == "false" || text == "0")
        return false;
    usage_error(where + " requires true/false, got `" + text + "`");
}

pf_magnifier_distribution_t parse_distribution(const std::string& text) {
    if (text == "uniform")
        return PF_MAGNIFIER_UNIFORM;
    if (text == "cosine")
        return PF_MAGNIFIER_COSINE;
    if (text == "triangle")
        return PF_MAGNIFIER_TRIANGLE;
    if (text == "endpoints")
        return PF_MAGNIFIER_ENDPOINTS;
    if (text == "front_loaded")
        return PF_MAGNIFIER_FRONT_LOADED;
    if (text == "back_loaded")
        return PF_MAGNIFIER_BACK_LOADED;
    usage_error("unknown magnifier distribution `" + text + "`");
}

pfh::CandidatePolicy parse_candidate_policy(const std::string& text) {
    if (text == "sampler_default")
        return pfh::CandidatePolicy::SamplerDefault;
    if (text == "without_replacement")
        return pfh::CandidatePolicy::WithoutReplacement;
    if (text == "exhaustive")
        return pfh::CandidatePolicy::Exhaustive;
    usage_error("--candidate-policy must be sampler_default, without_replacement, or exhaustive");
}

void print_help() {
    std::cout << "pineforge-hpo-native — native PineForge HPO runner\n\n"
              << "Usage:\n"
              << "  pineforge-hpo-native run --strategy FILE --ohlcv FILE "
                 "--objective EXPR [options]\n\n"
              << "Search-space options (repeatable):\n"
              << "  --int-dim NAME LOW HIGH STEP\n"
              << "  --log-int-dim NAME LOW HIGH\n"
              << "  --real-dim NAME LOW HIGH STEP|continuous\n"
              << "  --log-real-dim NAME LOW HIGH\n"
              << "  --bool-dim NAME\n"
              << "  --categorical-choice NAME VALUE          string choice\n"
              << "  --categorical-int-choice NAME VALUE      integer choice\n"
              << "  --categorical-real-choice NAME VALUE     real choice\n"
              << "  --categorical-bool-choice NAME VALUE     boolean choice\n"
              << "  --fixed-input NAME VALUE\n"
              << "  --strategy-override NAME VALUE\n\n"
              << "Study options:\n"
              << "  --sampler grid|random|tpe|dlib_global  default: grid\n"
              << "  --candidate-policy sampler_default|without_replacement|exhaustive\n"
              << "  --max-trials N              0 means all grid candidates\n"
              << "  --seed N                    sampler seed; dlib max 2147483647\n"
              << "  --workers N                 trial threads and adaptive batch size\n"
              << "  --direction maximize|minimize\n"
              << "  --constraint EXPR           comparison expression; repeatable\n"
              << "  --division-by-zero reject|ieee\n"
              << "  --non-finite reject|allow\n\n"
              << "TPE options:\n"
              << "  --tpe-startup-trials N      random observations before model fitting\n"
              << "  --tpe-ei-candidates N       candidates scored by log l(x)/g(x)\n"
              << "  --tpe-gamma-fraction X      good-observation fraction in (0, 1]\n"
              << "  --tpe-gamma-cap N           maximum good observations\n"
              << "  --tpe-prior-weight X        positive Parzen prior weight\n"
              << "  --tpe-constant-liar BOOL    place outstanding trials in g(x)\n\n"
              << "Backtest options:\n"
              << "  --input-tf TF --script-tf TF --chart-timezone TZ\n"
              << "  --bar-magnifier true|false --magnifier-samples N\n"
              << "  --magnifier-distribution "
                 "uniform|cosine|triangle|endpoints|front_loaded|back_loaded\n\n"
              << "Output options:\n"
              << "  --output FILE               also write the result JSON to FILE\n"
              << "  --artifact-key KEY          provenance label included in output\n";
}

void add_categorical_choice(Options& options, std::string name, pfh::ParameterValue value) {
    if (options.categorical_choices.find(name) == options.categorical_choices.end()) {
        options.categorical_order.push_back(name);
    }
    options.categorical_choices[std::move(name)].push_back(std::move(value));
}

Options parse_options(int argc, char** argv) {
    if (argc == 1 || std::string(argv[1]) == "--help" || std::string(argv[1]) == "-h") {
        print_help();
        std::exit(0);
    }
    if (std::string(argv[1]) != "run")
        usage_error("expected `run` subcommand");

    Options out;
    for (int i = 2; i < argc; ++i) {
        const std::string option = argv[i];
        if (option == "--strategy") {
            out.strategy = require_value(argc, argv, i, option);
        } else if (option == "--ohlcv") {
            out.ohlcv = require_value(argc, argv, i, option);
        } else if (option == "--output") {
            out.output = require_value(argc, argv, i, option);
        } else if (option == "--artifact-key") {
            out.artifact_key = require_value(argc, argv, i, option);
        } else if (option == "--objective") {
            out.objective = require_value(argc, argv, i, option);
        } else if (option == "--constraint") {
            out.constraints.push_back(require_value(argc, argv, i, option));
        } else if (option == "--sampler") {
            out.sampler = require_value(argc, argv, i, option);
            if (out.sampler != "grid" && out.sampler != "random" && out.sampler != "tpe" &&
                out.sampler != "dlib_global") {
                usage_error("--sampler must be grid, random, tpe, or dlib_global");
            }
        } else if (option == "--candidate-policy") {
            out.candidate_policy = parse_candidate_policy(require_value(argc, argv, i, option));
        } else if (option == "--max-trials") {
            out.max_trials = parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--seed") {
            out.seed = parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--workers") {
            const auto value = parse_u64(require_value(argc, argv, i, option), option);
            if (value == 0 || value > std::numeric_limits<unsigned>::max()) {
                usage_error("--workers must be between 1 and the platform unsigned maximum");
            }
            out.workers = static_cast<unsigned>(value);
        } else if (option == "--direction") {
            const auto value = require_value(argc, argv, i, option);
            if (value == "maximize")
                out.direction = Direction::kMaximize;
            else if (value == "minimize")
                out.direction = Direction::kMinimize;
            else
                usage_error("--direction must be maximize or minimize");
        } else if (option == "--tpe-startup-trials") {
            out.tpe_config.startup_trials = parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-ei-candidates") {
            out.tpe_config.ei_candidates = parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-gamma-fraction") {
            out.tpe_config.gamma_fraction =
                parse_double(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-gamma-cap") {
            out.tpe_config.gamma_cap = parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-prior-weight") {
            out.tpe_config.prior_weight =
                parse_double(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-constant-liar") {
            out.tpe_config.constant_liar = parse_bool(require_value(argc, argv, i, option), option);
        } else if (option == "--input-tf") {
            out.input_timeframe = require_value(argc, argv, i, option);
        } else if (option == "--script-tf") {
            out.script_timeframe = require_value(argc, argv, i, option);
        } else if (option == "--chart-timezone") {
            out.chart_timezone = require_value(argc, argv, i, option);
        } else if (option == "--bar-magnifier") {
            out.bar_magnifier = parse_bool(require_value(argc, argv, i, option), option);
        } else if (option == "--magnifier-samples") {
            const auto value = parse_i64(require_value(argc, argv, i, option), option);
            if (value <= 0 || value > std::numeric_limits<int>::max()) {
                usage_error("--magnifier-samples must be a positive int");
            }
            out.magnifier_samples = static_cast<int>(value);
        } else if (option == "--magnifier-distribution") {
            out.magnifier_distribution = parse_distribution(require_value(argc, argv, i, option));
        } else if (option == "--division-by-zero") {
            const auto value = require_value(argc, argv, i, option);
            if (value == "reject") {
                out.evaluation_policy.division_by_zero = pfh::DivisionByZeroPolicy::Reject;
            } else if (value == "ieee") {
                out.evaluation_policy.division_by_zero = pfh::DivisionByZeroPolicy::Ieee754;
            } else {
                usage_error("--division-by-zero must be reject or ieee");
            }
        } else if (option == "--non-finite") {
            const auto value = require_value(argc, argv, i, option);
            const auto policy =
                value == "allow" ? pfh::NonFinitePolicy::Allow : pfh::NonFinitePolicy::Reject;
            if (value != "allow" && value != "reject") {
                usage_error("--non-finite must be reject or allow");
            }
            out.evaluation_policy.non_finite_metric = policy;
            out.evaluation_policy.non_finite_result = policy;
        } else if (option == "--fixed-input") {
            const auto name = require_value(argc, argv, i, option);
            const auto value = require_value(argc, argv, i, option);
            if (!out.fixed_inputs.emplace(name, value).second) {
                usage_error("duplicate fixed input `" + name + "`");
            }
        } else if (option == "--strategy-override") {
            const auto name = require_value(argc, argv, i, option);
            const auto value = require_value(argc, argv, i, option);
            if (!out.strategy_overrides.emplace(name, value).second) {
                usage_error("duplicate strategy override `" + name + "`");
            }
        } else if (option == "--int-dim") {
            const auto name = require_value(argc, argv, i, option);
            const auto low = parse_i64(require_value(argc, argv, i, option), option);
            const auto high = parse_i64(require_value(argc, argv, i, option), option);
            const auto step = parse_i64(require_value(argc, argv, i, option), option);
            out.dimensions.emplace_back(pfh::IntegerDimension(name, low, high, step));
        } else if (option == "--log-int-dim") {
            const auto name = require_value(argc, argv, i, option);
            const auto low = parse_i64(require_value(argc, argv, i, option), option);
            const auto high = parse_i64(require_value(argc, argv, i, option), option);
            out.dimensions.emplace_back(pfh::IntegerDimension(name, low, high, 1, true));
        } else if (option == "--real-dim") {
            const auto name = require_value(argc, argv, i, option);
            const auto low = parse_double(require_value(argc, argv, i, option), option);
            const auto high = parse_double(require_value(argc, argv, i, option), option);
            const auto step_text = require_value(argc, argv, i, option);
            if (step_text == "continuous") {
                out.dimensions.emplace_back(pfh::RealDimension(name, low, high));
            } else {
                out.dimensions.emplace_back(
                    pfh::RealDimension(name, low, high, parse_double(step_text, option)));
            }
        } else if (option == "--log-real-dim") {
            const auto name = require_value(argc, argv, i, option);
            const auto low = parse_double(require_value(argc, argv, i, option), option);
            const auto high = parse_double(require_value(argc, argv, i, option), option);
            out.dimensions.emplace_back(pfh::RealDimension(name, low, high, std::nullopt, true));
        } else if (option == "--bool-dim") {
            out.dimensions.emplace_back(
                pfh::BooleanDimension(require_value(argc, argv, i, option)));
        } else if (option == "--categorical-choice") {
            const auto name = require_value(argc, argv, i, option);
            const auto value = require_value(argc, argv, i, option);
            add_categorical_choice(out, name, value);
        } else if (option == "--categorical-int-choice") {
            const auto name = require_value(argc, argv, i, option);
            const auto value = parse_i64(require_value(argc, argv, i, option), option);
            add_categorical_choice(out, name, value);
        } else if (option == "--categorical-real-choice") {
            const auto name = require_value(argc, argv, i, option);
            const auto value = parse_double(require_value(argc, argv, i, option), option);
            add_categorical_choice(out, name, value);
        } else if (option == "--categorical-bool-choice") {
            const auto name = require_value(argc, argv, i, option);
            const auto value = parse_bool(require_value(argc, argv, i, option), option);
            add_categorical_choice(out, name, value);
        } else {
            usage_error("unknown option `" + option + "`");
        }
    }

    if (out.strategy.empty())
        usage_error("--strategy is required");
    if (out.ohlcv.empty())
        usage_error("--ohlcv is required");
    if (out.objective.empty())
        usage_error("--objective is required");
    if ((out.sampler == "random" || out.sampler == "tpe" || out.sampler == "dlib_global") &&
        out.max_trials == 0) {
        usage_error(out.sampler + " sampling requires --max-trials > 0");
    }
    if (out.candidate_policy != pfh::CandidatePolicy::SamplerDefault && out.sampler != "tpe" &&
        out.sampler != "grid") {
        usage_error("finite candidate policies are supported only by tpe and grid samplers");
    }
    if (out.candidate_policy != pfh::CandidatePolicy::SamplerDefault && out.max_trials == 0) {
        usage_error("finite candidate policies require --max-trials > 0");
    }
    if (out.sampler == "dlib_global" &&
        out.seed > static_cast<std::uint64_t>(std::numeric_limits<std::int32_t>::max())) {
        usage_error("dlib_global requires --seed in [0, 2147483647]");
    }
    if (out.tpe_config.startup_trials == 0)
        usage_error("--tpe-startup-trials must be greater than zero");
    if (out.tpe_config.ei_candidates == 0 || out.tpe_config.ei_candidates > 1'000'000)
        usage_error("--tpe-ei-candidates must be between 1 and 1000000");
    if (!(out.tpe_config.gamma_fraction > 0.0 && out.tpe_config.gamma_fraction <= 1.0))
        usage_error("--tpe-gamma-fraction must be in (0, 1]");
    if (out.tpe_config.gamma_cap == 0)
        usage_error("--tpe-gamma-cap must be greater than zero");
    if (!(out.tpe_config.prior_weight > 0.0))
        usage_error("--tpe-prior-weight must be greater than zero");
    for (const auto& name : out.categorical_order) {
        out.dimensions.emplace_back(
            pfh::CategoricalDimension(name, out.categorical_choices.at(name)));
    }
    return out;
}

std::string json_escape(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char ch : value) {
        switch (ch) {
        case '"':
            out << "\\\"";
            break;
        case '\\':
            out << "\\\\";
            break;
        case '\b':
            out << "\\b";
            break;
        case '\f':
            out << "\\f";
            break;
        case '\n':
            out << "\\n";
            break;
        case '\r':
            out << "\\r";
            break;
        case '\t':
            out << "\\t";
            break;
        default:
            if (ch < 0x20) {
                out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                    << static_cast<int>(ch) << std::dec << std::setfill(' ');
            } else {
                out << static_cast<char>(ch);
            }
        }
    }
    return out.str();
}

std::string json_number(double value) {
    if (!std::isfinite(value))
        return "null";
    std::ostringstream out;
    out << std::setprecision(17) << value;
    return out.str();
}

std::string parameter_json(const pfh::ParameterValue& value) {
    return std::visit(
        [](const auto& item) -> std::string {
            using T = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<T, std::string>) {
                return "\"" + json_escape(item) + "\"";
            } else if constexpr (std::is_same_v<T, bool>) {
                return item ? "true" : "false";
            } else if constexpr (std::is_same_v<T, double>) {
                return json_number(item);
            } else {
                return std::to_string(item);
            }
        },
        value);
}

pfh::MetricMap collect_metrics(const pfh::ReportSnapshot& report,
                               const std::vector<pfh::MetricExpression*>& expressions,
                               std::map<std::string, double>* ordered) {
    pfh::MetricMap metrics;
    for (const auto* expression : expressions) {
        for (const auto& identifier : expression->identifiers()) {
            if (metrics.find(identifier) != metrics.end())
                continue;
            const auto value = report.metric(identifier);
            if (value.has_value()) {
                metrics.emplace(identifier, *value);
                if (ordered)
                    ordered->emplace(identifier, *value);
            }
        }
    }
    return metrics;
}

void validate_metric_identifiers(const std::vector<pfh::MetricExpression*>& expressions) {
    const pfh::ReportSnapshot schema;
    for (const auto* expression : expressions) {
        for (const auto& identifier : expression->identifiers()) {
            if (!schema.metric(identifier).has_value()) {
                throw std::invalid_argument("unknown report metric in expression: " + identifier);
            }
        }
    }
}

std::vector<pfh::Candidate> generate_candidates(const Options& options,
                                                const pfh::SearchSpace& space) {
    std::unique_ptr<pfh::Sampler> sampler;
    if (options.sampler == "grid") {
        sampler = std::make_unique<pfh::GridSampler>(space);
    } else if (options.sampler == "random") {
        sampler = std::make_unique<pfh::RandomSampler>(space, options.seed, options.max_trials);
    } else {
        throw std::logic_error("adaptive samplers cannot pre-generate candidates");
    }

    std::vector<pfh::Candidate> candidates;
    while (options.max_trials == 0 || candidates.size() < options.max_trials) {
        auto candidate = sampler->next();
        if (!candidate)
            break;
        candidates.push_back(std::move(*candidate));
    }
    if (candidates.empty())
        throw std::runtime_error("sampler produced no candidates");
    return candidates;
}

bool better(Direction direction, double candidate, double current) {
    return direction == Direction::kMaximize ? candidate > current : candidate < current;
}

TrialRecord evaluate_candidate(const pfh::Candidate& candidate,
                               const pfh::SearchSpace& space,
                               const Options& options,
                               const pfh::TrialExecutor& executor,
                               const pfh::MetricExpression& objective,
                               const std::vector<pfh::MetricExpression>& constraints,
                               const std::vector<pfh::MetricExpression*>& expressions,
                               const pfh::EvaluationPolicy& constraint_policy) {
    TrialRecord record;
    record.trial_id = candidate.id;
    record.candidate = candidate;
    try {
        auto serialized = space.serialize_candidate(record.candidate);
        for (const auto& [key, value] : options.fixed_inputs) {
            if (!serialized.emplace(key, value).second) {
                throw std::invalid_argument("fixed input overlaps search dimension: " + key);
            }
        }
        const auto execution = executor.execute(serialized, options.strategy_overrides);
        if (!execution.succeeded()) {
            record.status = "engine_error";
            record.error = execution.error;
            return record;
        }
        record.total_trades = execution.report.total_trades;
        record.net_profit = execution.report.net_profit;
        record.input_bars_processed = execution.report.input_bars_processed;
        record.script_bars_processed = execution.report.script_bars_processed;
        record.input_tf_seconds = execution.report.input_tf_seconds;
        record.script_tf_seconds = execution.report.script_tf_seconds;
        record.script_tf_ratio = execution.report.script_tf_ratio;
        record.needs_aggregation = execution.report.needs_aggregation;
        auto metric_map = collect_metrics(execution.report, expressions, &record.metrics);
        const auto score = objective.evaluate(metric_map, options.evaluation_policy);
        if (!score.valid) {
            record.status = "objective_error";
            record.error = score.diagnostic;
            return record;
        }
        if (!std::isfinite(score.value)) {
            record.status = "objective_error";
            record.error = "objective result is non-finite and cannot be ranked";
            return record;
        }
        record.objective = score.value;
        record.feasible = true;
        for (const auto& constraint : constraints) {
            const auto result = constraint.evaluate(metric_map, constraint_policy);
            if (!result.valid) {
                record.status = "constraint_error";
                record.error = result.diagnostic;
                record.feasible = false;
                break;
            }
            if (!std::isfinite(result.value)) {
                record.status = "constraint_error";
                record.error = "constraint result is non-finite";
                record.feasible = false;
                break;
            }
            if (result.value == 0.0)
                record.feasible = false;
        }
        if (record.status == "pending") {
            record.status = record.feasible ? "ok" : "constraint_violation";
        }
    } catch (const std::exception& error) {
        record.status = "trial_error";
        record.error = error.what();
    }
    return record;
}

std::vector<TrialRecord> evaluate_batch(const std::vector<pfh::Candidate>& candidates,
                                        unsigned requested_workers,
                                        const pfh::SearchSpace& space,
                                        const Options& options,
                                        const pfh::TrialExecutor& executor,
                                        const pfh::MetricExpression& objective,
                                        const std::vector<pfh::MetricExpression>& constraints,
                                        const std::vector<pfh::MetricExpression*>& expressions,
                                        const pfh::EvaluationPolicy& constraint_policy) {
    if (candidates.empty())
        return {};

    std::vector<TrialRecord> trials(candidates.size());
    std::atomic<std::size_t> next{0};
    const auto worker_count =
        static_cast<unsigned>(std::min<std::size_t>(requested_workers, candidates.size()));
    std::vector<std::thread> workers;
    workers.reserve(worker_count);
    try {
        for (unsigned worker = 0; worker < worker_count; ++worker) {
            workers.emplace_back([&]() {
                for (;;) {
                    const std::size_t index = next.fetch_add(1, std::memory_order_relaxed);
                    if (index >= candidates.size())
                        return;
                    trials[index] =
                        evaluate_candidate(candidates[index], space, options, executor, objective,
                                           constraints, expressions, constraint_policy);
                }
            });
        }
    } catch (...) {
        next.store(candidates.size(), std::memory_order_relaxed);
        for (auto& worker : workers) {
            if (worker.joinable())
                worker.join();
        }
        throw;
    }
    for (auto& worker : workers)
        worker.join();
    return trials;
}

std::string render_results(const Options& options,
                           const pfh::SearchSpace& space,
                           const std::optional<std::uint64_t>& finite_cardinality,
                           const std::vector<TrialRecord>& trials,
                           const std::optional<std::size_t>& best_index,
                           std::uint64_t duplicate_proposals_skipped) {
    const auto sampler_implementation = [&]() -> const char* {
        if (options.sampler == "tpe") {
            return options.candidate_policy == pfh::CandidatePolicy::SamplerDefault
                       ? "pineforge_product_tpe_v2"
                       : "pineforge_product_tpe_v2_finite";
        }
        if (options.sampler == "dlib_global")
            return "dlib_global_function_search_20.0.1";
        if (options.sampler == "random")
            return "pineforge_mt19937_64_random_v2";
        return "pineforge_grid_v2_finite";
    }();

    std::optional<std::uint64_t> unique_candidates;
    if (finite_cardinality.has_value()) {
        std::unordered_set<std::uint64_t> ordinals;
        ordinals.reserve(trials.size());
        for (const auto& trial : trials) {
            ordinals.insert(space.candidate_ordinal(trial.candidate));
        }
        unique_candidates = static_cast<std::uint64_t>(ordinals.size());
    }
    const std::uint64_t trials_requested =
        options.max_trials != 0 ? options.max_trials : finite_cardinality.value_or(0);
    const bool search_space_exhausted =
        finite_cardinality.has_value() && unique_candidates == finite_cardinality;
    const bool full_parameter_coverage = search_space_exhausted;
    const bool terminal_objective_coverage =
        std::all_of(trials.begin(), trials.end(), [](const TrialRecord& trial) {
            return trial.status == "ok" || trial.status == "constraint_violation";
        });
    const bool exhaustive_equivalent = full_parameter_coverage && terminal_objective_coverage;
    const char* stop_reason =
        search_space_exhausted
            ? "search_space_exhausted"
            : (trials.size() >= trials_requested ? "trial_budget_reached" : "sampler_stopped");
    std::ostringstream out;
    out << "{\n"
        << "  \"schema_version\": 1,\n"
        << "  \"pineforge_hpo_version\": \"" << PINEFORGE_HPO_VERSION << "\",\n"
        << "  \"sampler_implementation\": \"" << sampler_implementation << "\",\n"
        << "  \"ok\": " << (best_index ? "true" : "false") << ",\n"
        << "  \"artifact_key\": \"" << json_escape(options.artifact_key) << "\",\n"
        << "  \"sampler\": \"" << json_escape(options.sampler) << "\",\n"
        << "  \"candidate_policy\": \"" << pfh::candidate_policy_name(options.candidate_policy)
        << "\",\n"
        << "  \"candidate_policy_implementation\": \""
        << (options.candidate_policy == pfh::CandidatePolicy::SamplerDefault
                ? "sampler_default"
                : "pineforge_finite_space_v1")
        << "\",\n"
        << "  \"seed\": " << options.seed << ",\n"
        << "  \"sampler_config\": ";
    if (options.sampler == "tpe") {
        out << "{\"startup_trials\": " << options.tpe_config.startup_trials
            << ", \"ei_candidates\": " << options.tpe_config.ei_candidates
            << ", \"gamma_fraction\": " << json_number(options.tpe_config.gamma_fraction)
            << ", \"gamma_cap\": " << options.tpe_config.gamma_cap
            << ", \"prior_weight\": " << json_number(options.tpe_config.prior_weight)
            << ", \"constant_liar\": " << (options.tpe_config.constant_liar ? "true" : "false")
            << "},\n";
    } else {
        out << "{},\n";
    }
    out << "  \"direction\": \""
        << (options.direction == Direction::kMaximize ? "maximize" : "minimize") << "\",\n"
        << "  \"search_space_finite\": " << (finite_cardinality ? "true" : "false") << ",\n"
        << "  \"search_space_cardinality\": ";
    if (finite_cardinality)
        out << *finite_cardinality;
    else
        out << "null";
    out << ",\n  \"trials_requested\": " << trials_requested << ",\n"
        << "  \"trials_completed\": " << trials.size() << ",\n"
        << "  \"unique_candidates_attempted\": ";
    if (unique_candidates)
        out << *unique_candidates;
    else
        out << "null";
    out << ",\n  \"duplicate_proposals_skipped\": " << duplicate_proposals_skipped
        << ",\n  \"remaining_candidates\": ";
    if (finite_cardinality && unique_candidates)
        out << (*finite_cardinality - *unique_candidates);
    else
        out << "null";
    out << ",\n  \"search_space_exhausted\": " << (search_space_exhausted ? "true" : "false")
        << ",\n"
        << "  \"stop_reason\": \"" << stop_reason << "\",\n"
        << "  \"full_parameter_coverage\": " << (full_parameter_coverage ? "true" : "false")
        << ",\n"
        << "  \"exhaustive_equivalent\": " << (exhaustive_equivalent ? "true" : "false") << ",\n"
        << "  \"best_trial_id\": ";
    if (best_index)
        out << trials[*best_index].trial_id;
    else
        out << "null";
    out << ",\n  \"best_value\": ";
    if (best_index && trials[*best_index].objective) {
        out << json_number(*trials[*best_index].objective);
    } else {
        out << "null";
    }
    out << ",\n  \"trials\": [\n";

    for (std::size_t i = 0; i < trials.size(); ++i) {
        const auto& trial = trials[i];
        out << "    {\"trial_id\": " << trial.trial_id << ", \"status\": \""
            << json_escape(trial.status)
            << "\", \"feasible\": " << (trial.feasible ? "true" : "false") << ", \"objective\": ";
        if (trial.objective)
            out << json_number(*trial.objective);
        else
            out << "null";
        out << ", \"total_trades\": " << trial.total_trades
            << ", \"net_profit\": " << json_number(trial.net_profit)
            << ", \"backtest\": {\"input_bars_processed\": " << trial.input_bars_processed
            << ", \"script_bars_processed\": " << trial.script_bars_processed
            << ", \"input_tf_seconds\": " << trial.input_tf_seconds
            << ", \"script_tf_seconds\": " << trial.script_tf_seconds
            << ", \"script_tf_ratio\": " << trial.script_tf_ratio
            << ", \"needs_aggregation\": " << (trial.needs_aggregation ? "true" : "false") << "}"
            << ", \"parameters\": {";
        std::size_t parameter_index = 0;
        for (const auto& [name, value] : trial.candidate.values) {
            if (parameter_index++)
                out << ", ";
            out << "\"" << json_escape(name) << "\": " << parameter_json(value);
        }
        out << "}, \"metrics\": {";
        std::size_t metric_index = 0;
        for (const auto& [name, value] : trial.metrics) {
            if (metric_index++)
                out << ", ";
            out << "\"" << json_escape(name) << "\": " << json_number(value);
        }
        out << "}, \"error\": \"" << json_escape(trial.error) << "\"}";
        if (i + 1 != trials.size())
            out << ',';
        out << '\n';
    }
    out << "  ]\n}\n";
    return out.str();
}

int run(const Options& options) {
    pfh::SearchSpace space(options.dimensions);
    const auto finite_cardinality = space.finite_cardinality();
    if (options.sampler == "grid" && !finite_cardinality.has_value()) {
        throw std::invalid_argument(
            "grid sampling requires a step on every varying real dimension");
    }
    if (options.candidate_policy != pfh::CandidatePolicy::SamplerDefault) {
        if (!finite_cardinality.has_value()) {
            throw std::invalid_argument(
                "finite candidate policy requires a step on every varying real dimension");
        }
        if (options.max_trials > *finite_cardinality) {
            throw std::invalid_argument(
                "finite candidate budget must not exceed search-space cardinality");
        }
        if (options.candidate_policy == pfh::CandidatePolicy::Exhaustive &&
            options.max_trials != *finite_cardinality) {
            throw std::invalid_argument(
                "exhaustive candidate budget must equal search-space cardinality");
        }
    }
    pfh::MetricExpression objective(options.objective);
    std::vector<pfh::MetricExpression> constraints;
    constraints.reserve(options.constraints.size());
    for (const auto& source : options.constraints)
        constraints.emplace_back(source);
    std::vector<pfh::MetricExpression*> expressions{&objective};
    for (auto& constraint : constraints)
        expressions.push_back(&constraint);
    validate_metric_identifiers(expressions);

    auto plugin = std::make_shared<pfh::StrategyPlugin>(options.strategy);
    auto dataset = std::make_shared<pfh::Dataset>(pfh::Dataset::load_csv(options.ohlcv));
    pfh::BacktestConfiguration configuration;
    configuration.input_timeframe = options.input_timeframe;
    configuration.script_timeframe = options.script_timeframe;
    configuration.chart_timezone = options.chart_timezone;
    configuration.bar_magnifier = options.bar_magnifier;
    configuration.magnifier_samples = options.magnifier_samples;
    configuration.magnifier_distribution = options.magnifier_distribution;
    configuration.capture_equity_curve = false;
    const pfh::TrialExecutor executor(plugin, dataset, configuration);
    pfh::EvaluationPolicy constraint_policy = options.evaluation_policy;
    constraint_policy.division_by_zero = pfh::DivisionByZeroPolicy::Reject;
    constraint_policy.non_finite_metric = pfh::NonFinitePolicy::Reject;
    constraint_policy.non_finite_result = pfh::NonFinitePolicy::Reject;

    std::vector<TrialRecord> trials;
    std::uint64_t duplicate_proposals_skipped = 0;
    const auto direction = options.direction == Direction::kMaximize
                               ? pfh::ObjectiveDirection::Maximize
                               : pfh::ObjectiveDirection::Minimize;
    const auto evaluate_adaptive = [&](auto& sampler) {
        while (trials.size() < options.max_trials) {
            const auto remaining = options.max_trials - trials.size();
            const auto batch_size =
                static_cast<std::size_t>(std::min<std::uint64_t>(options.workers, remaining));
            std::vector<pfh::Candidate> candidates;
            candidates.reserve(batch_size);
            for (std::size_t index = 0; index < batch_size; ++index) {
                auto candidate = sampler.ask();
                if (!candidate)
                    break;
                candidates.push_back(std::move(*candidate));
            }
            if (candidates.empty())
                break;

            auto batch = evaluate_batch(candidates, options.workers, space, options, executor,
                                        objective, constraints, expressions, constraint_policy);
            // Proposal state is coordinator-owned. Ordered feedback makes a fixed
            // seed and worker count deterministic even if workers finish in a
            // different order. Only genuine feasible observations train the model;
            // failures and constraint violations are abandoned instead of being
            // assigned fabricated objective values.
            for (const auto& trial : batch) {
                if (trial.feasible && trial.objective && std::isfinite(*trial.objective)) {
                    sampler.tell(trial.trial_id, *trial.objective);
                } else {
                    sampler.abandon(trial.trial_id);
                }
            }
            for (auto& trial : batch)
                trials.push_back(std::move(trial));
        }
    };

    if (options.sampler == "dlib_global") {
        pfh::DlibGlobalSampler sampler(space, options.seed, direction, options.max_trials);
        evaluate_adaptive(sampler);
    } else if (options.sampler == "tpe") {
        pfh::TpeSampler sampler(space, options.seed, direction, options.max_trials,
                                options.tpe_config, options.candidate_policy);
        evaluate_adaptive(sampler);
        duplicate_proposals_skipped = sampler.duplicate_proposals_skipped();
    } else {
        auto candidates = generate_candidates(options, space);
        trials = evaluate_batch(candidates, options.workers, space, options, executor, objective,
                                constraints, expressions, constraint_policy);
    }

    if (trials.empty())
        throw std::runtime_error("sampler produced no candidates");

    std::optional<std::size_t> best_index;
    for (std::size_t i = 0; i < trials.size(); ++i) {
        if (!trials[i].feasible || !trials[i].objective || !std::isfinite(*trials[i].objective)) {
            continue;
        }
        if (!best_index ||
            better(options.direction, *trials[i].objective, *trials[*best_index].objective)) {
            best_index = i;
        }
    }

    const auto json = render_results(options, space, finite_cardinality, trials, best_index,
                                     duplicate_proposals_skipped);
    std::cout << json;
    if (!options.output.empty()) {
        std::ofstream output(options.output, std::ios::binary | std::ios::trunc);
        if (!output) {
            throw std::runtime_error("cannot open output file: " + options.output.string());
        }
        output << json;
        if (!output) {
            throw std::runtime_error("failed writing output file: " + options.output.string());
        }
    }
    return best_index ? 0 : 2;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        return run(parse_options(argc, argv));
    } catch (const std::exception& error) {
        std::cerr << "pineforge-hpo-native: " << error.what() << '\n';
        return 1;
    }
}
