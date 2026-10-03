#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/objective.hpp>
#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>
#include <pineforge/hpo/trial_executor.hpp>
#include <pineforge/hpo/types.hpp>

#include "json.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <deque>
#include <filesystem>
#include <functional>
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

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <unistd.h>

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
    std::filesystem::path syminfo;
    std::string objective;
    std::vector<std::string> constraints;
    std::vector<std::string> recorded_metrics;
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
    int progress_fd = -1;
    double max_wall_seconds = 0.0;
    double trial_timeout_seconds = 0.0;
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
    std::int64_t magnifier_sample_ticks_total = 0;
    std::int32_t input_tf_seconds = 0;
    std::int32_t script_tf_seconds = 0;
    std::int32_t script_tf_ratio = 0;
    bool needs_aggregation = false;
    std::map<std::string, double> metrics;
};

static_assert(std::atomic<bool>::is_always_lock_free);
std::atomic<bool> signal_cancelled{false};

extern "C" void request_stop(int) {
    signal_cancelled.store(true, std::memory_order_relaxed);
}

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
              << "  --record-metric PATH        additional report metric; repeatable\n"
              << "  --max-wall-seconds S        positive study wall limit; cooperative stop\n"
              << "  --trial-timeout-seconds T   positive per-trial limit; exits 3 on timeout\n\n"
              << "TPE options:\n"
              << "  --tpe-startup-trials N      random observations before model fitting\n"
              << "  --tpe-ei-candidates N       candidates scored by log l(x)/g(x)\n"
              << "  --tpe-gamma-fraction X      good-observation fraction in (0, 1]\n"
              << "  --tpe-gamma-cap N           maximum good observations\n"
              << "  --tpe-prior-weight X        positive Parzen prior weight\n"
              << "  --tpe-constant-liar BOOL    place outstanding trials in g(x)\n\n"
              << "Backtest options:\n"
              << "  --syminfo FILE              flat or wrapped instrument JSON\n"
              << "  --input-tf TF --script-tf TF --chart-timezone TZ\n"
              << "  --bar-magnifier true|false --magnifier-samples N\n"
              << "  --magnifier-distribution "
                 "uniform|cosine|triangle|endpoints|front_loaded|back_loaded\n\n"
              << "Output options:\n"
              << "  --output FILE               also write the result JSON to FILE\n"
              << "  --progress-fd N             terminal trial JSONL; drain while running\n"
              << "  --artifact-key KEY          provenance label included in output\n\n"
              << "SIGTERM/SIGINT stop new trials (stop_reason cancelled); "
                 "completed trials remain.\n"
              << "Study wall cap: stop_reason deadline. "
                 "Trial cap: status/stop_reason trial_timeout.\n"
              << "Exit codes: 0 best feasible trial; 1 initialization/I/O error;\n"
              << "            2 no feasible trial; 3 trial timeout (no worker join).\n";
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
        } else if (option == "--syminfo") {
            out.syminfo = require_value(argc, argv, i, option);
        } else if (option == "--progress-fd") {
            const auto descriptor = parse_i64(require_value(argc, argv, i, option), option);
            if (descriptor < 0 || descriptor > std::numeric_limits<int>::max())
                usage_error("--progress-fd requires a non-negative file descriptor");
            out.progress_fd = static_cast<int>(descriptor);
            const int flags = ::fcntl(out.progress_fd, F_GETFL);
            if (flags < 0 || (flags & O_ACCMODE) == O_RDONLY)
                usage_error("--progress-fd must be an open writable descriptor");
        } else if (option == "--max-wall-seconds" || option == "--trial-timeout-seconds") {
            const double seconds = parse_double(require_value(argc, argv, i, option), option);
            if (seconds <= 0.0)
                usage_error(option + " requires a positive number");
            if (option == "--max-wall-seconds")
                out.max_wall_seconds = seconds;
            else
                out.trial_timeout_seconds = seconds;
        } else if (option == "--record-metric") {
            out.recorded_metrics.push_back(require_value(argc, argv, i, option));
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

pfh::SymbolInfo read_symbol_info(const std::filesystem::path& file) {
    std::ifstream input(file, std::ios::binary);
    if (!input)
        throw std::invalid_argument("cannot open symbol info: " + file.string());
    std::string content;
    char buffer[4096];
    while (input.read(buffer, sizeof(buffer)) || input.gcount() != 0) {
        content.append(buffer, static_cast<std::size_t>(input.gcount()));
        if (content.size() > 1024 * 1024)
            throw std::invalid_argument("symbol info exceeds 1 MiB");
    }
    if (input.bad())
        throw std::invalid_argument("cannot read symbol info: " + file.string());
    const auto document = pfh::detail::parse_json(content);
    const auto* wrapped = document.find("syminfo");
    const auto& symbol = wrapped ? *wrapped : document;
    pfh::SymbolInfo info;
    const auto number = [&](const char* name) -> std::optional<double> {
        const auto* value = symbol.find(name);
        if (!value)
            return std::nullopt;
        const double parsed = value->real();
        if (parsed <= 0.0)
            throw std::invalid_argument(std::string("syminfo.") + name + " must be positive");
        return parsed;
    };
    const auto text = [&](const char* name) -> std::string {
        const auto* value = symbol.find(name);
        if (!value)
            return {};
        auto parsed = value->text();
        if (parsed.find('\0') != std::string::npos)
            throw std::invalid_argument(std::string("syminfo.") + name + " contains NUL");
        return parsed;
    };
    info.mintick = number("mintick");
    info.pointvalue = number("pointvalue");
    info.timezone = text("timezone");
    info.session = text("session");
    return info;
}

TrialRecord make_trial_record(const pfh::Candidate& candidate, const Options& options) {
    TrialRecord record;
    record.trial_id = candidate.id;
    record.candidate = candidate;
    for (const auto& name : options.recorded_metrics)
        record.metrics.emplace(name, std::numeric_limits<double>::quiet_NaN());
    return record;
}

std::string render_trial(const TrialRecord& trial) {
    std::ostringstream out;
    out << "{\"trial_id\": " << trial.trial_id << ", \"status\": \""
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
        << ", \"needs_aggregation\": " << (trial.needs_aggregation ? "true" : "false")
        << ", \"magnifier_sample_ticks_total\": " << trial.magnifier_sample_ticks_total << "}"
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
    return out.str();
}

enum class StopReason : std::uint8_t { kNone, kCancelled, kDeadline, kTrialTimeout };

class RunState final {
public:
    using Clock = std::chrono::steady_clock;

    RunState(const Options& options, Clock::time_point started,
             std::function<void(const std::vector<TrialRecord>&)> timeout_result)
        : options_(options), started_(started), timeout_result_(std::move(timeout_result)) {
        if (options_.progress_fd >= 0 || options_.trial_timeout_seconds > 0.0)
            writer_ = std::thread([this] { watch(); });
    }

    ~RunState() { shutdown(); }

    bool stopped() {
        StopReason expected = StopReason::kNone;
        if (signal_cancelled.load(std::memory_order_relaxed)) {
            reason_.compare_exchange_strong(expected, StopReason::kCancelled);
        } else if (options_.max_wall_seconds > 0.0 &&
                   seconds_since(started_) >= options_.max_wall_seconds) {
            reason_.compare_exchange_strong(expected, StopReason::kDeadline);
        }
        return reason_.load() != StopReason::kNone;
    }

    bool begin(const pfh::Candidate& candidate) {
        if (options_.trial_timeout_seconds == 0.0)
            return !stopped();
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopped())
            return false;
        active_.emplace(candidate.id, ActiveTrial{candidate, Clock::now()});
        changed_.notify_one();
        return true;
    }

    void finish(const TrialRecord& record) {
        if (options_.progress_fd < 0 && options_.trial_timeout_seconds == 0.0)
            return;
        std::lock_guard<std::mutex> lock(mutex_);
        if (timed_out_)
            return;
        active_.erase(record.trial_id);
        if (options_.trial_timeout_seconds > 0.0)
            completed_.emplace(record.trial_id, record);
        if (options_.progress_fd >= 0 && !progress_failed_)
            pending_.push_back(render_trial(record) + "\n");
        changed_.notify_one();
    }

    void shutdown() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            done_ = true;
            changed_.notify_one();
        }
        if (writer_.joinable())
            writer_.join();
    }

    void check_error() const {
        if (!error_.empty())
            throw std::runtime_error(error_);
    }

    const char* stop_reason() const {
        switch (reason_.load()) {
        case StopReason::kCancelled: return "cancelled";
        case StopReason::kDeadline: return "deadline";
        case StopReason::kTrialTimeout: return "trial_timeout";
        default: return "";
        }
    }

private:
    struct ActiveTrial {
        pfh::Candidate candidate;
        Clock::time_point started;
    };

    static double seconds_since(Clock::time_point started) {
        return std::chrono::duration<double>(Clock::now() - started).count();
    }

    void write_progress(const std::string& line) {
        std::size_t written = 0;
        while (written < line.size()) {
            const auto count = ::write(options_.progress_fd, line.data() + written,
                                       line.size() - written);
            if (count < 0 && errno == EINTR)
                continue;
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                struct pollfd descriptor {options_.progress_fd, POLLOUT, 0};
                int ready;
                do {
                    ready = ::poll(&descriptor, 1, -1);
                } while (ready < 0 && errno == EINTR);
                if (ready > 0 && (descriptor.revents & POLLOUT))
                    continue;
                throw std::runtime_error("failed waiting for writable --progress-fd");
            }
            if (count <= 0)
                throw std::runtime_error("failed writing terminal trial to --progress-fd");
            written += static_cast<std::size_t>(count);
        }
    }

    void watch() noexcept {
        try {
            for (;;) {
                std::unique_lock<std::mutex> lock(mutex_);
                changed_.wait_for(lock, std::chrono::milliseconds(5),
                                  [&] { return done_ || !pending_.empty(); });
                if (!timed_out_ && options_.trial_timeout_seconds > 0.0) {
                    for (const auto& [trial_id, trial] : active_) {
                        if (seconds_since(trial.started) < options_.trial_timeout_seconds)
                            continue;
                        timed_out_ = true;
                        reason_.store(StopReason::kTrialTimeout);
                        auto record = make_trial_record(trial.candidate, options_);
                        record.status = "trial_timeout";
                        record.error = "trial exceeded --trial-timeout-seconds";
                        completed_.emplace(trial_id, record);
                        if (options_.progress_fd >= 0 && !progress_failed_)
                            pending_.push_back(render_trial(record) + "\n");
                        break;
                    }
                }
                if (!pending_.empty()) {
                    auto line = std::move(pending_.front());
                    pending_.pop_front();
                    lock.unlock();
                    try {
                        write_progress(line);
                    } catch (const std::exception& error) {
                        std::lock_guard<std::mutex> failed_lock(mutex_);
                        error_ = error.what();
                        progress_failed_ = true;
                        pending_.clear();
                        StopReason expected = StopReason::kNone;
                        reason_.compare_exchange_strong(expected, StopReason::kCancelled);
                    }
                    continue;
                }
                if (timed_out_) {
                    std::vector<TrialRecord> trials;
                    trials.reserve(completed_.size());
                    for (const auto& [trial_id, record] : completed_) {
                        (void)trial_id;
                        trials.push_back(record);
                    }
                    lock.unlock();
                    if (!error_.empty()) {
                        std::cerr << "pineforge-hpo-native: " << error_ << '\n';
                        std::cerr.flush();
                    }
                    timeout_result_(trials);
                    ::_exit(3);
                }
                if (done_)
                    return;
            }
        } catch (const std::exception& error) {
            if (timed_out_) {
                std::cerr << "pineforge-hpo-native: " << error.what() << '\n';
                std::cerr.flush();
                ::_exit(3);
            }
            std::lock_guard<std::mutex> lock(mutex_);
            error_ = error.what();
            reason_.store(StopReason::kCancelled);
        }
    }

    const Options& options_;
    Clock::time_point started_;
    std::function<void(const std::vector<TrialRecord>&)> timeout_result_;
    std::atomic<StopReason> reason_{StopReason::kNone};
    std::mutex mutex_;
    std::condition_variable changed_;
    std::map<std::uint64_t, ActiveTrial> active_;
    std::map<std::uint64_t, TrialRecord> completed_;
    std::deque<std::string> pending_;
    std::string error_;
    bool progress_failed_ = false;
    bool timed_out_ = false;
    bool done_ = false;
    std::thread writer_;
};

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
                    ordered->insert_or_assign(identifier, *value);
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
                                                const pfh::SearchSpace& space,
                                                RunState& state) {
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
        if (state.stopped())
            break;
        auto candidate = sampler->next();
        if (!candidate)
            break;
        candidates.push_back(std::move(*candidate));
    }
    if (candidates.empty() && !state.stopped())
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
    TrialRecord record = make_trial_record(candidate, options);
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
        record.magnifier_sample_ticks_total = execution.report.magnifier_sample_ticks_total;
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
                                        const pfh::EvaluationPolicy& constraint_policy,
                                        RunState& state) {
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
                    if (state.stopped())
                        return;
                    const std::size_t index = next.fetch_add(1, std::memory_order_relaxed);
                    if (index >= candidates.size())
                        return;
                    if (!state.begin(candidates[index]))
                        return;
                    trials[index] =
                        evaluate_candidate(candidates[index], space, options, executor, objective,
                                           constraints, expressions, constraint_policy);
                    state.finish(trials[index]);
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
    trials.erase(std::remove_if(trials.begin(), trials.end(), [](const TrialRecord& trial) {
        return trial.status == "pending";
    }), trials.end());
    return trials;
}

std::string render_results(const Options& options,
                           const pfh::SearchSpace& space,
                           const std::optional<std::uint64_t>& finite_cardinality,
                           const std::vector<TrialRecord>& trials,
                           const std::optional<std::size_t>& best_index,
                           std::uint64_t duplicate_proposals_skipped,
                           const std::string& requested_stop_reason = {}) {
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
    const std::string stop_reason = !requested_stop_reason.empty()
        ? requested_stop_reason
        : search_space_exhausted
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
        out << "    " << render_trial(trials[i]);
        if (i + 1 != trials.size())
            out << ',';
        out << '\n';
    }
    out << "  ]\n}\n";
    return out.str();
}

std::optional<std::size_t> best_trial(const Options& options,
                                     const std::vector<TrialRecord>& trials) {
    std::optional<std::size_t> best_index;
    for (std::size_t index = 0; index < trials.size(); ++index) {
        if (!trials[index].feasible || !trials[index].objective ||
            !std::isfinite(*trials[index].objective))
            continue;
        if (!best_index || better(options.direction, *trials[index].objective,
                                  *trials[*best_index].objective))
            best_index = index;
    }
    return best_index;
}

void write_results(const Options& options, const std::string& json) {
    std::cout << json;
    std::cout.flush();
    if (!std::cout)
        throw std::runtime_error("failed writing result JSON to stdout");
    if (!options.output.empty()) {
        std::ofstream output(options.output, std::ios::binary | std::ios::trunc);
        if (!output)
            throw std::runtime_error("cannot open output file: " + options.output.string());
        output << json;
        output.flush();
        if (!output)
            throw std::runtime_error("failed writing output file: " + options.output.string());
    }
}

int run(const Options& options) {
    const auto started = RunState::Clock::now();
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
    std::vector<pfh::MetricExpression> recorded_metrics;
    recorded_metrics.reserve(options.recorded_metrics.size());
    for (const auto& name : options.recorded_metrics) {
        if (!pfh::ReportSnapshot{}.metric(name))
            throw std::invalid_argument("unknown report metric to record: " + name);
        recorded_metrics.emplace_back(name);
    }
    for (auto& metric : recorded_metrics)
        expressions.push_back(&metric);
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
    if (!options.syminfo.empty())
        configuration.symbol_info = read_symbol_info(options.syminfo);
    const pfh::TrialExecutor executor(plugin, dataset, configuration);
    pfh::EvaluationPolicy constraint_policy = options.evaluation_policy;
    constraint_policy.division_by_zero = pfh::DivisionByZeroPolicy::Reject;
    constraint_policy.non_finite_metric = pfh::NonFinitePolicy::Reject;
    constraint_policy.non_finite_result = pfh::NonFinitePolicy::Reject;

    std::vector<TrialRecord> trials;
    std::atomic<std::uint64_t> duplicate_proposals_skipped{0};
    RunState state(options, started, [&](const std::vector<TrialRecord>& completed) {
        write_results(options, render_results(options, space, finite_cardinality, completed,
                                             best_trial(options, completed),
                                             duplicate_proposals_skipped.load(),
                                             "trial_timeout"));
    });
    const auto direction = options.direction == Direction::kMaximize
                               ? pfh::ObjectiveDirection::Maximize
                               : pfh::ObjectiveDirection::Minimize;
    const auto evaluate_adaptive = [&](auto& sampler) {
        while (trials.size() < options.max_trials) {
            if (state.stopped())
                break;
            const auto remaining = options.max_trials - trials.size();
            const auto batch_size =
                static_cast<std::size_t>(std::min<std::uint64_t>(options.workers, remaining));
            std::vector<pfh::Candidate> candidates;
            candidates.reserve(batch_size);
            for (std::size_t index = 0; index < batch_size; ++index) {
                if (state.stopped())
                    break;
                auto candidate = sampler.ask();
                if (!candidate)
                    break;
                candidates.push_back(std::move(*candidate));
            }
            if (candidates.empty())
                break;
            if constexpr (std::is_same_v<std::decay_t<decltype(sampler)>, pfh::TpeSampler>)
                duplicate_proposals_skipped.store(sampler.duplicate_proposals_skipped());

            auto batch = evaluate_batch(candidates, options.workers, space, options, executor,
                                        objective, constraints, expressions, constraint_policy,
                                        state);
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
        duplicate_proposals_skipped.store(sampler.duplicate_proposals_skipped());
    } else {
        auto candidates = generate_candidates(options, space, state);
        trials = evaluate_batch(candidates, options.workers, space, options, executor, objective,
                                constraints, expressions, constraint_policy, state);
    }

    state.shutdown();
    if (trials.empty() && !state.stopped())
        throw std::runtime_error("sampler produced no candidates");

    const auto best_index = best_trial(options, trials);

    const auto json = render_results(options, space, finite_cardinality, trials, best_index,
                                     duplicate_proposals_skipped.load(), state.stop_reason());
    write_results(options, json);
    state.check_error();
    return best_index ? 0 : 2;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        struct sigaction action {};
        action.sa_handler = request_stop;
        action.sa_flags = SA_RESTART;
        sigemptyset(&action.sa_mask);
        if (::sigaction(SIGTERM, &action, nullptr) != 0 ||
            ::sigaction(SIGINT, &action, nullptr) != 0)
            throw std::runtime_error("cannot install cooperative stop handlers");
        action.sa_handler = SIG_IGN;
        if (::sigaction(SIGPIPE, &action, nullptr) != 0)
            throw std::runtime_error("cannot install progress I/O error handler");
        return run(parse_options(argc, argv));
    } catch (const std::exception& error) {
        std::cerr << "pineforge-hpo-native: " << error.what() << '\n';
        return 1;
    }
}
