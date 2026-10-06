#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/objective.hpp>
#include <pineforge/hpo/pruner.hpp>
#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>
#include <pineforge/hpo/trial_executor.hpp>
#include <pineforge/hpo/types.hpp>

#include "../core/ordinal_set.hpp"
#include "batch_executor.hpp"
#include "continuation.hpp"
#include "failure_json.hpp"
#include "json.hpp"
#include "symbol_feeds.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_set>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
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
    std::filesystem::path trials_file;
    std::filesystem::path warm_start;
    std::shared_ptr<const pfh::detail::WarmHistory> warm_history;
    std::shared_ptr<const std::string> space_json;
    std::string space_hash;
    bool tpe_warm_restored = false;
    std::string tpe_sampler_state;
    std::string numeric_build_identity;
    std::string parent_numeric_build_identity;
    std::string trials_out = "all";
    std::uint64_t best_k = 10;
    std::filesystem::path syminfo;
    std::filesystem::path symbol_feeds_path;
    std::filesystem::path symbol_feeds_spec;
    std::shared_ptr<const pfh::SymbolFeeds> symbol_feeds;
    std::optional<pfh::detail::Json> symbol_feeds_record;
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
    std::uint64_t batch_size = 0;
    unsigned batch_lag = 0;
    std::filesystem::path scheduler_stats;
    pfh::PrunerKind pruner = pfh::PrunerKind::None;
    std::string pruner_name = "none";
    std::vector<double> pruner_rungs{0.25, 0.5};
    unsigned pruner_eta = 2;
    bool bar_magnifier = false;
    int magnifier_samples = 4;
    pf_magnifier_distribution_t magnifier_distribution = PF_MAGNIFIER_ENDPOINTS;
    Direction direction = Direction::kMaximize;
    pfh::EvaluationPolicy evaluation_policy;
    pfh::TpeSamplerConfig tpe_config;
};

struct TrialRecord {
    std::shared_ptr<const std::string> space_json;
    std::string space_hash;
    bool tpe_enabled = false;
    std::optional<std::uint64_t> tpe_history_switch;
    std::uint64_t trial_id = 0;
    pfh::Candidate candidate;
    std::string status = "pending";
    std::string error;
    pfh::detail::FailureDetails failure;
    bool feasible = false;
    std::optional<double> objective;
    std::vector<std::optional<double>> constraint_values;
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
    std::vector<std::optional<double>> rung_scores;
    std::int64_t bars_processed_total = 0;
    std::int64_t script_bars_processed_total = 0;
    std::int64_t magnifier_ticks_total = 0;
    std::optional<double> pruning_cut;
    bool pruning_enabled = false;
};

static_assert(std::atomic<bool>::is_always_lock_free);
std::atomic<bool> signal_cancelled{false};

extern "C" void request_stop(int) {
    signal_cancelled.store(true, std::memory_order_relaxed);
}

[[noreturn]] void usage_error(const std::string& message) {
    throw pfh::TypedHpoError<std::invalid_argument>(
        "hpo_cli_usage", {}, message + "\nRun `pineforge-hpo-native --help` for usage.");
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
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_cli_usage", {},
                                                            "trailing characters");
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
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_cli_usage", {},
                                                            "trailing characters");
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
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_cli_usage", {},
                                                            "invalid finite number");
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
                 "--objective EXPR [options]\n"
              << "  pineforge-hpo-native space-info --spec FILE [--warm-start FILE] "
              << "[--symbol-feeds FILE]\n"
              << "  pineforge-hpo-native warm-encode --spec FILE --input FILE "
                 "--output FILE [--block-trials N] [--symbol-feeds FILE]\n\n"
              << "Warm history options:\n"
              << "  --warm-start FILE           binary v2 blocks or v0.5 JSON/JSONL\n"
              << "  --warm-details              space-info: counts and next trial ID\n"
              << "  --warm-digest               space-info: compute warm object SHA-256\n"
              << "  --block-trials N            warm-encode: maximum rows per block\n\n"
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
              << "  --symbol-feeds FILE        fixed other-symbol feed index (loaded once)\n"
              << "  --strategy-override NAME VALUE\n\n"
              << "Study options:\n"
              << "  --sampler grid|random|tpe|dlib_global  default: grid\n"
              << "  --candidate-policy sampler_default|without_replacement|exhaustive\n"
              << "  --max-trials N              0: grid exhaustive; adaptive deadline only\n"
              << "  --seed N                    sampler seed; dlib max 2147483647\n"
              << "  --workers N                 execution threads\n"
              << "  --batch-size N              proposals per batch; default: workers\n"
              << "  --batch-lag 0|1             fixed feedback lag; default: 0\n"
              << "  --scheduler-stats FILE      nondeterministic timing sidecar\n"
              << "  --trials-out all|best-k|none final trial retention; default: all\n"
              << "  --best-k N                  retained feasible winners; default: 10\n"
              << "  --trials-file FILE          flushed full terminal-trial NDJSON\n"
              << "  --pruner none|median|halving default: none\n"
              << "  --pruner-rungs F,F          increasing prefixes; default: 0.25,0.5\n"
              << "  --pruner-eta N              elimination factor >= 2; default: 2\n"
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
              << "  --tpe-max-threads N        fit/score workers; 0: min(8, available CPUs)\n"
              << "  --tpe-history-switch N     completed observations before bounded TPE"
                 " (default unset: never switch; JSON null)\n"
              << "  --tpe-scale-ei-candidates N acquisition draws after the history switch\n"
              << "  --tpe-bad-reservoir-size N  older non-elite reservoir (default 448)\n"
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
              << "            2 no feasible trial; 3 trial timeout (no worker join);\n"
              << "            4 warm-start incompatible; 5 space exhausted.\n";
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
        } else if (option == "--symbol-feeds") {
            out.symbol_feeds_path = require_value(argc, argv, i, option);
        } else if (option == "--symbol-feeds-spec") {
            out.symbol_feeds_spec = require_value(argc, argv, i, option);
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
        } else if (option == "--trials-out") {
            out.trials_out = require_value(argc, argv, i, option);
            if (out.trials_out != "all" && out.trials_out != "best-k" && out.trials_out != "none")
                usage_error("--trials-out must be all, best-k, or none");
        } else if (option == "--best-k") {
            out.best_k = parse_u64(require_value(argc, argv, i, option), option);
            if (out.best_k == 0 || out.best_k > 1'000'000)
                usage_error("--best-k must be between 1 and 1000000");
        } else if (option == "--trials-file") {
            out.trials_file = require_value(argc, argv, i, option);
        } else if (option == "--seed") {
            out.seed = parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--workers") {
            const auto value = parse_u64(require_value(argc, argv, i, option), option);
            if (value == 0 || value > std::numeric_limits<unsigned>::max()) {
                usage_error("--workers must be between 1 and the platform unsigned maximum");
            }
            out.workers = static_cast<unsigned>(value);
        } else if (option == "--warm-start") {
            out.warm_start = require_value(argc, argv, i, option);
        } else if (option == "--batch-size") {
            out.batch_size = parse_u64(require_value(argc, argv, i, option), option);
            if (out.batch_size == 0 || out.batch_size > 1'000'000)
                usage_error("--batch-size must be between 1 and 1000000");
        } else if (option == "--batch-lag") {
            const auto value = parse_u64(require_value(argc, argv, i, option), option);
            if (value > 1)
                usage_error("--batch-lag must be 0 or 1");
            out.batch_lag = static_cast<unsigned>(value);
        } else if (option == "--scheduler-stats") {
            out.scheduler_stats = require_value(argc, argv, i, option);
        } else if (option == "--pruner") {
            out.pruner_name = require_value(argc, argv, i, option);
            if (out.pruner_name == "none")
                out.pruner = pfh::PrunerKind::None;
            else if (out.pruner_name == "median")
                out.pruner = pfh::PrunerKind::Median;
            else if (out.pruner_name == "halving")
                out.pruner = pfh::PrunerKind::Halving;
            else
                usage_error("--pruner must be none, median, or halving");
        } else if (option == "--pruner-rungs") {
            const auto value = require_value(argc, argv, i, option);
            std::istringstream source(value);
            std::string fraction;
            out.pruner_rungs.clear();
            while (std::getline(source, fraction, ','))
                out.pruner_rungs.push_back(parse_double(fraction, option));
            if (out.pruner_rungs.empty() || value.back() == ',')
                usage_error("--pruner-rungs requires comma-separated fractions");
        } else if (option == "--pruner-eta") {
            const auto value = parse_u64(require_value(argc, argv, i, option), option);
            if (value < 2 || value > std::numeric_limits<unsigned>::max())
                usage_error("--pruner-eta must be at least 2 and fit unsigned");
            out.pruner_eta = static_cast<unsigned>(value);
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
        } else if (option == "--tpe-max-threads") {
            const auto count = parse_u64(require_value(argc, argv, i, option), option);
            if (count > 1024)
                usage_error("--tpe-max-threads must be between 0 and 1024");
            out.tpe_config.max_threads = static_cast<std::uint32_t>(count);
        } else if (option == "--tpe-history-switch") {
            out.tpe_config.history_switch =
                parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-scale-ei-candidates") {
            out.tpe_config.scale_ei_candidates =
                parse_u64(require_value(argc, argv, i, option), option);
        } else if (option == "--tpe-bad-reservoir-size") {
            out.tpe_config.bad_reservoir_size =
                parse_u64(require_value(argc, argv, i, option), option);
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
        out.max_trials == 0 && out.max_wall_seconds == 0.0) {
        usage_error(out.sampler + " sampling requires --max-trials or --max-wall-seconds");
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
    if (out.tpe_config.scale_ei_candidates == 0 ||
        out.tpe_config.scale_ei_candidates > 1'000'000)
        usage_error("--tpe-scale-ei-candidates must be between 1 and 1000000");
    if (out.tpe_config.history_switch && *out.tpe_config.history_switch == 0)
        usage_error("--tpe-history-switch must be positive");
    if (out.tpe_config.bad_reservoir_size > 65536)
        usage_error("--tpe-bad-reservoir-size must be between 0 and 65536");
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
        throw pfh::TypedHpoError<std::invalid_argument>(
            "hpo_input_file_invalid", {}, "cannot open symbol info: " + file.string());
    std::string content;
    char buffer[4096];
    while (input.read(buffer, sizeof(buffer)) || input.gcount() != 0) {
        content.append(buffer, static_cast<std::size_t>(input.gcount()));
        if (content.size() > 1024 * 1024)
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_input_file_invalid", {},
                                                            "symbol info exceeds 1 MiB");
    }
    if (input.bad())
        throw pfh::TypedHpoError<std::invalid_argument>(
            "hpo_input_file_invalid", {}, "cannot read symbol info: " + file.string());
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
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "backtest"}},
                std::string("syminfo.") + name + " must be positive");
        return parsed;
    };
    // The catalog carries `"mincontract": null` when the lot size is unknown: absent, no grid.
    // Anything else in the object that is read must be a positive finite JSON number, or the
    // parse fails.
    const auto lot_grid = [&]() -> std::optional<double> {
        const auto* value = symbol.find("mincontract");
        if (!value || value->kind == pfh::detail::Json::Kind::Null)
            return std::nullopt;
        const char* const message = "syminfo.mincontract must be a positive finite number";
        if (value->kind != pfh::detail::Json::Kind::Number)
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                            {{"reason", "study"}}, message);
        double parsed = 0.0;
        try {
            parsed = value->real();
        } catch (const std::exception&) {
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                            {{"reason", "study"}}, message);
        }
        if (!(parsed > 0.0))
            throw pfh::TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                            {{"reason", "study"}}, message);
        return parsed;
    };
    const auto text = [&](const char* name) -> std::string {
        const auto* value = symbol.find(name);
        if (!value)
            return {};
        auto parsed = value->text();
        if (parsed.find('\0') != std::string::npos)
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "backtest"}},
                std::string("syminfo.") + name + " contains NUL");
        return parsed;
    };
    info.mintick = number("mintick");
    info.pointvalue = number("pointvalue");
    info.mincontract = lot_grid();
    info.timezone = text("timezone");
    info.session = text("session");
    return info;
}

TrialRecord make_trial_record(const pfh::Candidate& candidate, const Options& options) {
    TrialRecord record;
    record.space_json = options.space_json;
    record.space_hash = options.space_hash;
    if (options.sampler == "tpe") {
        record.tpe_enabled = true;
        record.tpe_history_switch = options.tpe_config.history_switch;
    }
    record.trial_id = candidate.id;
    record.candidate = candidate;
    record.constraint_values.resize(options.constraints.size());
    record.pruning_enabled = options.pruner != pfh::PrunerKind::None;
    for (const auto& name : options.recorded_metrics)
        record.metrics.emplace(name, std::numeric_limits<double>::quiet_NaN());
    return record;
}

void reset_pruning_report(TrialRecord& record) {
    if (!record.pruning_enabled)
        return;
    record.feasible = false;
    record.objective.reset();
    record.total_trades = 0;
    record.net_profit = std::numeric_limits<double>::quiet_NaN();
    record.input_bars_processed = 0;
    record.script_bars_processed = 0;
    record.magnifier_sample_ticks_total = 0;
    record.input_tf_seconds = 0;
    record.script_tf_seconds = 0;
    record.script_tf_ratio = 0;
    record.needs_aggregation = false;
    for (auto& [name, value] : record.metrics)
        value = std::numeric_limits<double>::quiet_NaN();
}

std::string render_trial(const TrialRecord& trial) {
    std::ostringstream out;
    if (trial.tpe_enabled)
        out << "{\"tpe_history_switch\": "
            << (trial.tpe_history_switch ? std::to_string(*trial.tpe_history_switch) : "null")
            << ", ";
    else
        out << "{";
    out << "\"trial_id\": " << trial.trial_id << ", \"status\": \""
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
    out << "}, \"constraint_values\": [";
    for (std::size_t index = 0; index < trial.constraint_values.size(); ++index) {
        if (index)
            out << ',';
        out << (trial.constraint_values[index] ? json_number(*trial.constraint_values[index]) :
                "null");
    }
    out << "], \"metrics\": {";
    std::size_t metric_index = 0;
    for (const auto& [name, value] : trial.metrics) {
        if (metric_index++)
            out << ", ";
        out << "\"" << json_escape(name) << "\": " << json_number(value);
    }
    out << "}, \"error\": \"" << json_escape(pfh::detail::capped_error(trial.error)) << "\""
        << ", \"failure_code\": "
        << (trial.failure.code ? "\"" + json_escape(*trial.failure.code) + "\"" : "null")
        << ", \"failure_args\": "
        << pfh::detail::canonical_failure_args(trial.failure.args).value_or("null")
        << ", \"failure_origin\": "
        << (trial.failure.origin.empty() ? "null" : "\"" + trial.failure.origin + "\"");
    if (trial.pruning_enabled) {
        out << ", \"pruning\": {\"method\": \"prefix_rerun\", \"rungs_completed\": "
            << trial.rung_scores.size()
            << ", \"bars_processed_total\": " << trial.bars_processed_total
            << ", \"script_bars_processed_total\": " << trial.script_bars_processed_total
            << ", \"magnifier_sample_ticks_total\": " << trial.magnifier_ticks_total
            << ", \"cut\": "
            << (trial.pruning_cut ? json_number(*trial.pruning_cut) : "null")
            << ", \"rung_scores\": [";
        for (std::size_t index = 0; index < trial.rung_scores.size(); ++index) {
            if (index)
                out << ',';
            out << (trial.rung_scores[index] ? json_number(*trial.rung_scores[index]) : "null");
        }
        out << "]}";
    }
    if (trial.space_json)
        out << ", \"space_hash_version\": " << pfh::detail::space_hash_version
            << ", \"space_hash\": \"" << trial.space_hash << "\", \"space\": "
            << *trial.space_json;
    out << "}";
    return out.str();
}

enum class StopReason : std::uint8_t { kNone, kCancelled, kDeadline, kTrialTimeout };

class TrialArchive final {
public:
    TrialArchive(const Options& options, const pfh::SearchSpace& space, bool finite)
        : options_(options), space_(space), finite_(finite),
          ordinals_(finite ? space.finite_cardinality().value_or(0) : 0) {
        if (options.warm_history) {
            objective_coverage = options.warm_history->completed ==
                options.warm_history->size();
            if (finite_) {
                for (std::uint64_t row = 0; row < options.warm_history->size(); ++row)
                    ordinals_.insert(options.warm_history->ordinal(space, row));
            }
        }
    }

    void add(const TrialRecord& record) {
        ++completed;
        ++counts[record.status];
        objective_coverage = objective_coverage &&
            (record.status == "ok" || record.status == "constraint_violation");
        if (finite_ && (options_.warm_history || (options_.sampler != "grid" &&
            options_.candidate_policy == pfh::CandidatePolicy::SamplerDefault)))
            ordinals_.insert(space_.candidate_ordinal(record.candidate));
        if (options_.trials_out == "all")
            all_.push_back(record);
        if (!record.feasible || !record.objective || !std::isfinite(*record.objective))
            return;
        const auto compare = [&](const TrialRecord& left, const TrialRecord& right) {
            if (*left.objective != *right.objective)
                return options_.direction == Direction::kMaximize
                    ? *left.objective > *right.objective : *left.objective < *right.objective;
            return left.trial_id < right.trial_id;
        };
        if (best_.size() == options_.best_k) {
            if (!compare(record, best_.front()))
                return;
            std::pop_heap(best_.begin(), best_.end(), compare);
            best_.pop_back();
        }
        best_.push_back(record);
        std::push_heap(best_.begin(), best_.end(), compare);
    }

    std::vector<TrialRecord> retained() {
        auto records = options_.trials_out == "all" ? std::move(all_) : best_;
        std::sort(records.begin(), records.end(), [](const auto& left, const auto& right) {
            return left.trial_id < right.trial_id;
        });
        return records;
    }

    std::optional<std::uint64_t> unique() const {
        if (!finite_)
            return std::nullopt;
        if (options_.warm_history)
            return ordinals_.size();
        return options_.sampler == "grid" ||
               options_.candidate_policy != pfh::CandidatePolicy::SamplerDefault
            ? completed : ordinals_.size();
    }

    std::uint64_t completed = 0;
    std::map<std::string, std::uint64_t> counts;
    bool objective_coverage = true;

private:
    const Options& options_;
    const pfh::SearchSpace& space_;
    bool finite_;
    pfh::detail::OrdinalSet ordinals_;
    std::vector<TrialRecord> all_;
    std::vector<TrialRecord> best_;
};

class RunState final {
public:
    using Clock = std::chrono::steady_clock;

    RunState(const Options& options, Clock::time_point started, TrialArchive& archive,
             std::function<void(const std::vector<TrialRecord>&)> timeout_result)
        : options_(options), started_(started), archive_(archive),
          timeout_result_(std::move(timeout_result)),
          has_trials_file_(!options_.trials_file.empty()) {
        if (options.warm_history)
            next_progress_id_ = options.warm_history->next_id;
        if (!options_.trials_file.empty()) {
            trials_file_.open(options_.trials_file, std::ios::binary | std::ios::trunc);
            if (!trials_file_)
                throw pfh::TypedHpoError<std::runtime_error>("hpo_output_io_failed", {},
                                                             "cannot open --trials-file");
        }
        if (options_.progress_fd >= 0) {
            struct stat descriptor {};
            if (::fstat(options_.progress_fd, &descriptor) != 0)
                throw pfh::TypedHpoError<std::runtime_error>("hpo_output_io_failed", {},
                                                             "cannot inspect --progress-fd");
            if (S_ISFIFO(descriptor.st_mode)) {
                progress_atomic_limit_ = ::fpathconf(options_.progress_fd, _PC_PIPE_BUF);
                if (progress_atomic_limit_ <= 0)
                    progress_atomic_limit_ = PIPE_BUF;
            }
            progress_flags_ = ::fcntl(options_.progress_fd, F_GETFL);
            if (progress_flags_ < 0 ||
                ::fcntl(options_.progress_fd, F_SETFL, progress_flags_ | O_NONBLOCK) != 0)
                throw pfh::TypedHpoError<std::runtime_error>(
                    "hpo_output_io_failed", {}, "cannot make --progress-fd nonblocking");
        }
        if (options_.progress_fd >= 0 || has_trials_file_ ||
            options_.trial_timeout_seconds > 0.0)
            writer_ = std::thread([this] { watch(); });
    }

    ~RunState() {
        shutdown();
        if (progress_flags_ >= 0)
            ::fcntl(options_.progress_fd, F_SETFL, progress_flags_);
    }

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
        changed_.notify_all();
        return true;
    }

    void finish(const TrialRecord& record) {
        std::unique_lock<std::mutex> lock(mutex_);
        if (timed_out_)
            return;
        active_.erase(record.trial_id);
        const bool streaming = options_.progress_fd >= 0 || has_trials_file_;
        if (streaming && !progress_failed_) {
            const auto limit = 2 * (options_.batch_size ? options_.batch_size : options_.workers);
            changed_.wait(lock, [&] {
                return pending_.size() < limit || progress_failed_ || timed_out_ ||
                       record.trial_id == next_progress_id_;
            });
        }
        if (timed_out_)
            return;
        archive_.add(record);
        if (streaming && !progress_failed_) {
            const auto begin = Clock::now();
            pending_.emplace(record.trial_id, render_trial(record) + "\n");
            serialization_seconds_ += seconds_since(begin);
        }
        changed_.notify_all();
    }

    void skip(std::uint64_t trial_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (options_.progress_fd >= 0 || has_trials_file_)
            pending_.emplace(trial_id, "");
        changed_.notify_all();
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
        if (error_exception_)
            std::rethrow_exception(error_exception_);
    }

    double serialization_seconds() const noexcept { return serialization_seconds_; }
    double progress_write_seconds() const noexcept { return progress_write_seconds_; }
    std::uint64_t progress_bytes() const noexcept { return progress_bytes_; }

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
        if (line.empty())
            return;
        std::size_t written = 0;
        std::optional<Clock::time_point> stopped_at;
        while (options_.progress_fd >= 0 && written < line.size()) {
            const auto remaining = line.size() - written;
            const auto chunk_size = progress_atomic_limit_ > 0
                ? std::min(remaining, static_cast<std::size_t>(progress_atomic_limit_))
                : remaining;
            const auto count = ::write(options_.progress_fd, line.data() + written,
                                       chunk_size);
            if (count < 0 && errno == EINTR)
                continue;
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                struct pollfd descriptor {options_.progress_fd, POLLOUT, 0};
                int ready;
                do {
                    ready = ::poll(&descriptor, 1, 50);
                } while (ready < 0 && errno == EINTR);
                if (ready > 0 && (descriptor.revents & POLLOUT))
                    continue;
                if (ready == 0) {
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        check_timeouts();
                    }
                    if (stopped()) {
                        if (!stopped_at)
                            stopped_at = Clock::now();
                        if (seconds_since(*stopped_at) >= 2.0)
                            throw pfh::TypedHpoError<std::runtime_error>(
                                "hpo_output_io_failed", {},
                                "--progress-fd reader stalled after stop");
                    }
                    continue;
                }
                throw pfh::TypedHpoError<std::runtime_error>(
                    "hpo_output_io_failed", {}, "failed waiting for writable --progress-fd");
            }
            if (count <= 0)
                throw pfh::TypedHpoError<std::runtime_error>(
                    "hpo_output_io_failed", {}, "failed writing terminal trial to --progress-fd");
            written += static_cast<std::size_t>(count);
        }
        if (has_trials_file_) {
            trials_file_ << line;
            trials_file_.flush();
            if (!trials_file_)
                throw pfh::TypedHpoError<std::runtime_error>(
                    "hpo_output_io_failed", {}, "failed writing terminal trial to --trials-file");
        }
    }

    void check_timeouts() {
        if (timed_out_ || options_.trial_timeout_seconds == 0.0)
            return;
        for (const auto& [trial_id, trial] : active_) {
            if (seconds_since(trial.started) < options_.trial_timeout_seconds)
                continue;
            timed_out_ = true;
            reason_.store(StopReason::kTrialTimeout);
            auto record = make_trial_record(trial.candidate, options_);
            record.status = "trial_timeout";
            record.error = "trial exceeded --trial-timeout-seconds";
            record.failure = {"hpo_trial_timeout", "{}", "hpo"};
            archive_.add(record);
            if ((options_.progress_fd >= 0 || has_trials_file_) && !progress_failed_)
                pending_.emplace(trial_id, render_trial(record) + "\n");
            changed_.notify_all();
            break;
        }
    }

    void watch() noexcept {
        try {
            for (;;) {
                std::unique_lock<std::mutex> lock(mutex_);
                changed_.wait_for(lock, std::chrono::milliseconds(5),
                                  [&] { return done_ || pending_.count(next_progress_id_) != 0; });
                check_timeouts();
                auto next = pending_.find(next_progress_id_);
                if (next == pending_.end() && (timed_out_ || done_))
                    next = pending_.begin();
                if (next != pending_.end()) {
                    next_progress_id_ = next->first + 1;
                    auto line = std::move(next->second);
                    pending_.erase(next);
                    changed_.notify_all();
                    lock.unlock();
                    try {
                        const auto begin = Clock::now();
                        write_progress(line);
                        progress_write_seconds_ += seconds_since(begin);
                        progress_bytes_ += line.size();
                    } catch (const std::exception& error) {
                        std::lock_guard<std::mutex> failed_lock(mutex_);
                        error_ = error.what();
                        error_exception_ = std::current_exception();
                        progress_failed_ = true;
                        pending_.clear();
                        changed_.notify_all();
                        StopReason expected = StopReason::kNone;
                        reason_.compare_exchange_strong(expected, StopReason::kCancelled);
                    }
                    continue;
                }
                if (timed_out_) {
                    auto trials = archive_.retained();
                    lock.unlock();
                    check_error();
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
                std::cout << pfh::detail::failure_document(error, 3);
                std::cout.flush();
                ::_exit(3);
            }
            std::lock_guard<std::mutex> lock(mutex_);
            error_ = error.what();
            error_exception_ = std::current_exception();
            progress_failed_ = true;
            reason_.store(StopReason::kCancelled);
            changed_.notify_all();
        }
    }

    const Options& options_;
    Clock::time_point started_;
    TrialArchive& archive_;
    std::function<void(const std::vector<TrialRecord>&)> timeout_result_;
    std::atomic<StopReason> reason_{StopReason::kNone};
    std::mutex mutex_;
    std::condition_variable changed_;
    std::map<std::uint64_t, ActiveTrial> active_;
    std::map<std::uint64_t, std::string> pending_;
    std::uint64_t next_progress_id_ = 0;
    std::ofstream trials_file_;
    const bool has_trials_file_;
    int progress_flags_ = -1;
    long progress_atomic_limit_ = 0;
    std::string error_;
    std::exception_ptr error_exception_;
    bool progress_failed_ = false;
    bool timed_out_ = false;
    bool done_ = false;
    double serialization_seconds_ = 0.0;
    double progress_write_seconds_ = 0.0;
    std::uint64_t progress_bytes_ = 0;
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
                throw pfh::TypedHpoError<std::invalid_argument>(
                    "hpo_study_spec_invalid", {{"reason", "objective"}},
                    "unknown report metric in expression: " + identifier);
            }
        }
    }
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
                               const pfh::EvaluationPolicy& constraint_policy,
                               const pfh::Pruner& pruner,
                               const std::vector<std::size_t>& bar_counts,
                               const std::vector<std::optional<double>>& cuts) {
    TrialRecord record = make_trial_record(candidate, options);
    try {
        auto serialized = space.serialize_candidate(record.candidate);
        for (const auto& [key, value] : options.fixed_inputs) {
            if (!serialized.emplace(key, value).second) {
                throw pfh::TypedHpoError<std::invalid_argument>(
                    "hpo_study_spec_invalid", {{"reason", "input"}},
                    "fixed input overlaps search dimension: " + key);
            }
        }
        for (std::size_t rung = 0; rung < bar_counts.size(); ++rung) {
            const bool full_window = bar_counts[rung] == executor.dataset().size();
            const auto execution = executor.execute_prefix(
                serialized, options.strategy_overrides, bar_counts[rung]);
            record.bars_processed_total += execution.report.input_bars_processed;
            record.script_bars_processed_total += execution.report.script_bars_processed;
            record.magnifier_ticks_total += execution.report.magnifier_sample_ticks_total;
            if (!execution.succeeded()) {
                record.status = "engine_error";
                record.error = execution.error;
                record.failure = {execution.error_code, execution.error_args, "engine"};
                reset_pruning_report(record);
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
                record.failure = {"hpo_metric_expression_failed", "{}", "hpo"};
                reset_pruning_report(record);
                return record;
            }
            if (!std::isfinite(score.value)) {
                record.status = "objective_error";
                record.error = "objective result is non-finite and cannot be ranked";
                record.failure = {"hpo_metric_expression_failed", "{}", "hpo"};
                reset_pruning_report(record);
                return record;
            }
            record.objective = score.value;
            if (!full_window) {
                record.rung_scores.push_back(score.value);
                if (pruner.prune(score.value, cuts[rung])) {
                    record.status = "pruned";
                    record.pruning_cut = cuts[rung];
                    return record;
                }
                continue;
            }
            record.feasible = true;
            for (std::size_t index = 0; index < constraints.size(); ++index) {
                const auto result = constraints[index].evaluate(metric_map, constraint_policy);
                if (!result.valid) {
                    record.status = "constraint_error";
                    record.error = result.diagnostic;
                    record.failure = {"hpo_metric_expression_failed", "{}", "hpo"};
                    record.feasible = false;
                    break;
                }
                if (!std::isfinite(result.value)) {
                    record.status = "constraint_error";
                    record.error = "constraint result is non-finite";
                    record.failure = {"hpo_metric_expression_failed", "{}", "hpo"};
                    record.feasible = false;
                    break;
                }
                if (result.value == 0.0)
                    record.feasible = false;
                record.constraint_values[index] = result.value;
            }
            if (record.status == "pending")
                record.status = record.feasible ? "ok" : "constraint_violation";
        }
    } catch (const std::exception& error) {
        record.status = "trial_error";
        record.error = error.what();
        record.failure = pfh::detail::exception_failure(error);
    }
    return record;
}

std::string render_results(const Options& options,
                           const pfh::SearchSpace& space,
                           const std::optional<std::uint64_t>& finite_cardinality,
                           const std::vector<TrialRecord>& trials,
                           const std::optional<std::size_t>& best_index,
                           std::uint64_t duplicate_proposals_skipped,
                           const std::string& requested_stop_reason = {},
                           const TrialArchive* archive = nullptr) {
    const bool finite_space = std::all_of(space.dimensions().begin(), space.dimensions().end(),
        [](const pfh::Dimension& dimension) {
            return std::visit([](const auto& item) {
                using DimensionType = std::decay_t<decltype(item)>;
                if constexpr (std::is_same_v<DimensionType, pfh::RealDimension>)
                    return item.low() == item.high() || item.step().has_value();
                else
                    return true;
            }, dimension);
        });
    const auto sampler_implementation = [&]() -> const char* {
        if (options.sampler == "tpe") {
            return options.candidate_policy == pfh::CandidatePolicy::SamplerDefault
                       ? "pineforge_product_tpe_v3_bounded"
                       : "pineforge_product_tpe_v3_bounded_finite";
        }
        if (options.sampler == "dlib_global")
            return "dlib_global_function_search_20.0.1";
        if (options.sampler == "random")
            return "pineforge_mt19937_64_random_v2";
        return "pineforge_grid_v2_finite";
    }();

    std::optional<std::uint64_t> unique_candidates;
    if (archive) {
        unique_candidates = archive->unique();
    } else if (finite_cardinality.has_value()) {
        std::unordered_set<std::uint64_t> ordinals;
        ordinals.reserve(trials.size());
        for (const auto& trial : trials) {
            ordinals.insert(space.candidate_ordinal(trial.candidate));
        }
        unique_candidates = static_cast<std::uint64_t>(ordinals.size());
    }
    const std::uint64_t trials_requested =
        options.max_trials != 0 ? options.max_trials :
            (options.sampler == "grid" ? finite_cardinality.value_or(0) : 0);
    const std::uint64_t trials_completed = archive ? archive->completed : trials.size();
    const bool search_space_exhausted =
        finite_cardinality.has_value() && unique_candidates == finite_cardinality;
    const bool full_parameter_coverage = search_space_exhausted;
    const bool terminal_objective_coverage = archive ? archive->objective_coverage :
        std::all_of(trials.begin(), trials.end(), [](const TrialRecord& trial) {
            return trial.status == "ok" || trial.status == "constraint_violation";
        });
    const bool exhaustive_equivalent = full_parameter_coverage && terminal_objective_coverage;
    const std::string stop_reason = !requested_stop_reason.empty()
        ? requested_stop_reason
        : search_space_exhausted
            ? "search_space_exhausted"
            : (trials_requested && trials_completed >= trials_requested
                ? "trial_budget_reached" : "sampler_stopped");
    std::ostringstream out;
    out << "{\n"
        << "  \"schema_version\": 1,\n"
        << "  \"space_hash_version\": " << pfh::detail::space_hash_version << ",\n"
        << "  \"space_hash\": \"" << options.space_hash << "\",\n"
        << "  \"space\": " << *options.space_json << ",\n"
        << "  \"pineforge_hpo_version\": \"" << PINEFORGE_HPO_VERSION << "\",\n"
        << "  \"sampler_implementation\": \"" << sampler_implementation << "\",\n";
    if (options.symbol_feeds_record) {
        out << "  \"applied_runtime\": {\"symbol_feeds\": "
            << pfh::detail::dump_json(*options.symbol_feeds_record) << "},\n"
            << "  \"runtime_sha256\": \""
            << pfh::detail::symbol_feeds_identity(&*options.symbol_feeds_record) << "\",\n";
    }
    out
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
        << "  \"tpe_history_switch\": "
        << (options.sampler == "tpe" && options.tpe_config.history_switch
                ? std::to_string(*options.tpe_config.history_switch) : "null")
        << ",\n"
        << "  \"sampler_config\": ";
    if (options.sampler == "tpe") {
        out << "{\"startup_trials\": " << options.tpe_config.startup_trials
            << ", \"history_switch\": "
            << (options.tpe_config.history_switch
                    ? std::to_string(*options.tpe_config.history_switch) : "null")
            << ", \"scale_ei_candidates\": " << options.tpe_config.scale_ei_candidates
            << ", \"bad_reservoir_size\": " << options.tpe_config.bad_reservoir_size
            << ", \"ei_candidates\": " << options.tpe_config.ei_candidates
            << ", \"max_threads\": " << options.tpe_config.max_threads
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
        << "  \"batch_size\": " << (options.batch_size ? options.batch_size : options.workers)
        << ",\n  \"batch_lag\": " << options.batch_lag
        << ",\n  \"replay_contract\": \""
        << "ordered_batches_v1"
        << "\",\n  \"continuation_contract\": \""
        << (options.sampler == "tpe" ? "sampler_checkpoint_v2" : "ordered_batches_v1")
        << "\",\n"
        << "  \"pruner\": \"" << options.pruner_name << "\",\n"
        << "  \"pruner_eta\": " << options.pruner_eta << ",\n"
        << "  \"pruner_rungs\": [";
    for (std::size_t rung = 0; rung < options.pruner_rungs.size(); ++rung) {
        if (rung)
            out << ", ";
        out << json_number(options.pruner_rungs[rung]);
    }
    out << "],\n"
        << "  \"search_space_finite\": " << (finite_space ? "true" : "false") << ",\n"
        << "  \"search_space_cardinality_overflow\": "
        << (finite_space && !finite_cardinality ? "true" : "false") << ",\n"
        << "  \"search_space_cardinality\": ";
    if (finite_cardinality)
        out << *finite_cardinality;
    else
        out << "null";
    out << ",\n  \"trials_requested\": " << trials_requested << ",\n"
        << "  \"trials_completed\": " << trials_completed << ",\n"
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

    for (std::size_t i = 0; options.trials_out != "none" && i < trials.size(); ++i) {
        out << "    " << render_trial(trials[i]);
        if (i + 1 != trials.size())
            out << ',';
        out << '\n';
    }
    out << "  ]";
    if (archive && options.trials_out != "all") {
        out << ",\n  \"summary\": {\"counts_by_status\": {";
        std::size_t count_index = 0;
        for (const auto& [status, count] : archive->counts) {
            if (count_index++)
                out << ',';
            out << '"' << json_escape(status) << "\":" << count;
        }
        out << "},\"best_k\":[";
        for (std::size_t index = 0; index < trials.size(); ++index) {
            if (index)
                out << ',';
            out << render_trial(trials[index]);
        }
        out << "],\"space_coverage\":{\"search_space_finite\":"
            << (finite_space ? "true" : "false")
            << ",\"search_space_cardinality_overflow\":"
            << (finite_space && !finite_cardinality ? "true" : "false")
            << ",\"search_space_cardinality\":"
            << (finite_cardinality ? std::to_string(*finite_cardinality) : "null")
            << ",\"unique_candidates_attempted\":"
            << (unique_candidates ? std::to_string(*unique_candidates) : "null")
            << ",\"full_parameter_coverage\":" << (full_parameter_coverage ? "true" : "false")
            << ",\"exhaustive_equivalent\":" << (exhaustive_equivalent ? "true" : "false")
            << "}}";
    }
    if (!options.tpe_sampler_state.empty())
        out << ",\n  \"tpe_sampler_state\": \"" << json_escape(options.tpe_sampler_state) << "\"";
    const auto parent_identity_json = options.parent_numeric_build_identity.empty() ? "null" :
        "\"" + json_escape(options.parent_numeric_build_identity) + "\"";
    if (!options.numeric_build_identity.empty())
        out << ",\n  \"numeric_build_identity\": \"" << json_escape(options.numeric_build_identity)
            << "\",\n  \"parent_numeric_build_identity\": " << parent_identity_json;
    if (options.warm_history) {
        const auto& warm = *options.warm_history;
        out << ",\n  \"warm_start\": {\"source_sha256\":\"" << warm.source_digest()
            << "\",\"trials\":" << warm.size()
            << ",\"completed\":" << warm.completed << ",\"feasible\":" << warm.feasible
            << ",\"space_hash\":\"" << options.space_hash << "\"";
        if (!options.numeric_build_identity.empty())
            out << ",\"numeric_build_identity\":\"" << json_escape(options.numeric_build_identity)
                << "\",\"parent_numeric_build_identity\":" << parent_identity_json;
        out << '}';
        if (options.sampler == "tpe")
            out << ",\n  \"warm_start_model\":\""
                << (options.tpe_warm_restored ? "restored_sampler_state" : "rebuilt_history")
                << "\"";
        if (options.sampler == "tpe" && !options.tpe_warm_restored &&
            !options.parent_numeric_build_identity.empty() &&
            options.parent_numeric_build_identity != options.numeric_build_identity) {
            out << ",\n  \"warm_start_reason\":\"TPE numerical algorithm or build identity "
                   "changed; "
                << "rebuilt from objective history without importing the old sampler state "
                << "(v0.8.0 checkpoints are not bitwise continuations)\"";
        }
        if (options.trials_out == "all") {
            out << ",\n  \"warm_start_trials\": [";
            for (std::uint64_t index = 0; index < warm.size(); ++index) {
                if (index)
                    out << ',';
                out << pfh::detail::dump_json(warm.record(space, index));
            }
            out << ']';
        }
    }
    out << ",\n  \"trials_out\":\"" << options.trials_out << "\",\n"
        << "  \"best_k\":" << options.best_k << "\n}\n";
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

void write_result_file(const Options& options, const std::string& json) {
    if (!options.output.empty()) {
        std::ofstream output(options.output, std::ios::binary | std::ios::trunc);
        if (!output)
            throw pfh::TypedHpoError<std::runtime_error>(
                "hpo_output_io_failed", {}, "cannot open output file: " + options.output.string());
        output << json;
        output.flush();
        if (!output)
            throw pfh::TypedHpoError<std::runtime_error>(
                "hpo_output_io_failed", {},
                "failed writing output file: " + options.output.string());
    }
}

void publish_result(const std::string& json) {
    std::cout << json;
    std::cout.flush();
    if (!std::cout)
        throw pfh::TypedHpoError<std::runtime_error>("hpo_output_io_failed", {},
                                                     "failed writing result JSON to stdout");
}

void write_results(const Options& options, const std::string& json) {
    write_result_file(options, json);
    publish_result(json);
}

void validate_search_input_kinds(const Options& options) {
    const bool required = std::any_of(options.dimensions.begin(), options.dimensions.end(),
        [](const auto& dimension) {
            const auto* categorical = std::get_if<pfh::CategoricalDimension>(&dimension);
            return categorical && std::any_of(categorical->choices().begin(),
                categorical->choices().end(), [](const auto& choice) {
                    return std::holds_alternative<std::string>(choice);
                });
        });
    const auto manifest_path = options.strategy.parent_path() / "manifest.json";
    std::optional<pfh::detail::Json> manifest;
    try {
        manifest = pfh::detail::parse_json(pfh::detail::read_document(manifest_path),
                                         256 * 1024 * 1024);
        if (manifest->kind != pfh::detail::Json::Kind::Object)
            throw pfh::TypedHpoError<std::runtime_error>(
                "hpo_study_spec_invalid", {{"reason", "study"}}, "expected an object");
        const auto* inputs = manifest->find("inputs");
        if (!inputs || inputs->kind != pfh::detail::Json::Kind::Array)
            throw pfh::TypedHpoError<std::runtime_error>(
                "hpo_study_spec_invalid", {{"reason", "input"}}, "missing input metadata");
    } catch (const std::exception& error) {
        if (!required)
            return;
        throw pfh::TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "input_symbol"}},
            manifest_path.string() +
                ": cannot rule out input.symbol "
                "(D7): " +
                error.what() + "; engine/codegen >= 1.1.0 metadata is required");
    }
    const auto* kind_schema = manifest->find("input_kind_schema");
    const bool symbol_kinds = (kind_schema && kind_schema->kind == pfh::detail::Json::Kind::Number &&
                               kind_schema->value == "1");
    std::string codegen_version = "unknown";
    try {
        codegen_version = pfh::detail::field(pfh::detail::field(pfh::detail::field(
            *manifest, "request_identity"), "codegen"), "version").text();
    } catch (const std::exception&) {
    }
    const std::regex version_pattern(
        R"(^\s*[vV]?([0-9]+)(?:\.([0-9]+))?(?:\.([0-9]+))?([-+a-zA-Z.].*)?\s*$)");
    std::smatch version_match;
    const bool known_version = std::regex_match(codegen_version, version_match, version_pattern);
    const auto version_component = [&](unsigned index) {
        return known_version && version_match[index].matched ?
            std::stoull(version_match[index].str()) : 0ULL;
    };
    const auto codegen_major = version_component(1);
    const auto codegen_minor = version_component(2);
    const auto codegen_patch = version_component(3);
    const std::string version_suffix = known_version ? version_match[4].str() : "";
    const bool initial_prerelease = known_version && codegen_major == 1 &&
        codegen_minor == 1 && codegen_patch == 0 &&
        (version_suffix.find('-') == 0 || version_suffix.find('a') == 0 ||
         version_suffix.find('b') == 0 || version_suffix.find("rc") == 0 ||
         version_suffix.find("dev") == 0 || version_suffix.find(".dev") == 0);
    if (symbol_kinds && known_version &&
        (codegen_major < 1 || (codegen_major == 1 && codegen_minor < 1) || initial_prerelease)) {
        throw pfh::TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "input"}},
            "input_kind_schema: 1 contradicts recorded codegen version " + codegen_version +
                "; codegen >= 1.1.0 is required");
    }
    const auto provenance_path = options.strategy.parent_path() / "provenance.json";
    if (std::filesystem::is_regular_file(provenance_path)) {
        const auto provenance = pfh::detail::parse_json(
            pfh::detail::read_document(provenance_path), 256 * 1024 * 1024);
        for (const std::string field : {"input_kind_schema", "plugin_sha256", "artifact_key"}) {
            const auto* recorded = manifest->find(field);
            const auto* verified = provenance.find(field);
            if ((recorded == nullptr) != (verified == nullptr) ||
                (recorded && (recorded->kind != verified->kind ||
                              recorded->value != verified->value))) {
                throw pfh::TypedHpoError<std::invalid_argument>(
                    "hpo_study_spec_invalid", {{"reason", "artifact_metadata"}},
                    "artifact manifest disagrees with provenance: " + field);
            }
        }
        const auto* identity = provenance.find("request_identity");
        const auto* codegen = identity ? identity->find("codegen") : nullptr;
        const auto* version = codegen ? codegen->find("version") : nullptr;
        if (version && version->text() != codegen_version)
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "artifact_metadata"}},
                "artifact codegen version disagrees with provenance");
    }
    for (const auto& dimension : options.dimensions) {
        const auto* categorical = std::get_if<pfh::CategoricalDimension>(&dimension);
        const bool needs_kind = categorical && std::any_of(categorical->choices().begin(),
            categorical->choices().end(), [](const auto& choice) {
                return std::holds_alternative<std::string>(choice);
            });
        const pfh::detail::Json* selected = nullptr;
        for (const auto& input : manifest->find("inputs")->items) {
            if (input.kind != pfh::detail::Json::Kind::Object)
                continue;
            const auto* title = input.find("title");
            if (title && title->value == pfh::dimension_name(dimension)) {
                if (selected)
                    throw pfh::TypedHpoError<std::invalid_argument>(
                        "hpo_study_spec_invalid", {{"reason", "input"}},
                        manifest_path.string() + ": duplicate input " + title->value +
                            " used by search dimension");
                selected = &input;
            }
        }
        const auto* type = selected ? selected->find("type") : nullptr;
        const auto* kind = selected ? selected->find("kind") : nullptr;
        if (type && type->value == "string" && !symbol_kinds)
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "input_symbol"}},
                manifest_path.string() + ": cannot rule out input.symbol '" +
                    std::string(pfh::dimension_name(dimension)) +
                    "' (D7); manifest not stamped kind-capable by pineforge-hpo's builder; "
                    "rebuild the artifact with pineforge-hpo >= 0.8.0 and codegen >= 1.1.0; "
                    "codegen version " +
                    codegen_version);
        if ((kind && kind->value == "symbol") || (type && type->value == "symbol"))
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "input_symbol"}},
                "HPO over input.symbol '" + std::string(pfh::dimension_name(dimension)) +
                    "' is refused (D7); "
                    "only fixed other-symbol reads are supported");
        const bool ordinary_string = kind && kind->kind == pfh::detail::Json::Kind::String &&
            (kind->value == "string" || kind->value == "color" || kind->value == "timeframe" ||
             kind->value == "session" || kind->value == "text_area");
        const bool unknown_kind = kind && kind->kind != pfh::detail::Json::Kind::Null &&
                                  !ordinary_string;
        if (needs_kind && (!type || type->kind != pfh::detail::Json::Kind::String))
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "input"}},
                manifest_path.string() + ": missing input type for " +
                    std::string(pfh::dimension_name(dimension)));
        if (needs_kind && type->value != "int" && type->value != "float" &&
            type->value != "bool" && type->value != "string" &&
            type->value != "source" && type->value != "enum")
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "input"}},
                "search_space." + std::string(pfh::dimension_name(dimension)) +
                    ".choices[0] is incompatible with Pine input type '" + type->value + "'");
        if (type && type->value == "string" && unknown_kind)
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "input_symbol"}},
                manifest_path.string() + ": cannot rule out input.symbol '" +
                    std::string(pfh::dimension_name(dimension)) +
                    "' (D7); unrecognized input kind" + "; codegen version " + codegen_version);
    }
}

int run(Options options) {
    const auto started = RunState::Clock::now();
    validate_search_input_kinds(options);
    pfh::Pruner pruner(options.pruner, options.pruner_rungs, options.pruner_eta,
                       options.direction == Direction::kMinimize);
    if (options.batch_lag && options.sampler == "tpe" && !options.tpe_config.constant_liar)
        throw pfh::TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "lag-one TPE requires constant liar enabled");
    pfh::SearchSpace space(options.dimensions);
    std::optional<std::uint64_t> finite_cardinality;
    try {
        finite_cardinality = space.finite_cardinality();
    } catch (const std::overflow_error&) {
        if (options.sampler == "grid" ||
            options.candidate_policy != pfh::CandidatePolicy::SamplerDefault)
            throw;
    }
    if (options.sampler == "grid" && !finite_cardinality.has_value()) {
        throw pfh::TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "grid sampling requires a step on every varying real dimension");
    }
    const unsigned symbol_sources = static_cast<unsigned>(!options.symbol_feeds_path.empty()) +
        static_cast<unsigned>(!options.symbol_feeds_spec.empty());
    if (symbol_sources > 1)
        pfh::detail::symbol_feed_error("use a file or an inline index, not both");
    if (symbol_sources) {
        auto feeds = std::make_shared<const pfh::SymbolFeeds>(
            !options.symbol_feeds_spec.empty() ?
                pfh::detail::load_symbol_feeds_spec(options.symbol_feeds_spec) :
                pfh::detail::load_symbol_feeds(options.symbol_feeds_path));
        if (!feeds->empty()) {
            options.symbol_feeds_record = pfh::detail::symbol_feeds_record(*feeds);
            options.symbol_feeds = std::move(feeds);
        }
    }
    auto recorded = pfh::detail::recorded_space(space, options.objective,
        options.direction == Direction::kMaximize ? "maximize" : "minimize", options.constraints);
    if (options.symbol_feeds_record)
        recorded.members["symbol_feeds"] = *options.symbol_feeds_record;
    options.space_json = std::make_shared<const std::string>(pfh::detail::dump_json(recorded));
    options.space_hash = pfh::detail::space_hash(recorded);
    if (!options.warm_start.empty()) {
        if (options.sampler == "dlib_global")
            throw pfh::detail::WarmStartError("dlib_global continuation is not supported");
        auto warm = std::make_shared<pfh::detail::WarmHistory>(
            pfh::detail::load_warm_history(options.warm_start, space, recorded));
        if (warm->binary && options.pruner != pfh::PrunerKind::None)
            throw pfh::detail::WarmStartError("binary history has no pruning rungs");
        if (finite_cardinality && warm->tried_count() == *finite_cardinality)
            throw pfh::detail::SpaceExhausted();
        if (warm->next_id == std::numeric_limits<std::uint64_t>::max() ||
            options.max_trials > std::numeric_limits<std::uint64_t>::max() - warm->next_id)
            throw pfh::detail::WarmStartError("new trial budget would overflow trial IDs");
        for (const auto& scores : warm->rung_scores)
            pruner.observe(scores);
        options.warm_history = std::move(warm);
    }
    if (options.candidate_policy != pfh::CandidatePolicy::SamplerDefault) {
        if (!finite_cardinality.has_value()) {
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "study"}},
                "finite candidate policy requires a step on every varying real dimension");
        }
        const auto remaining = *finite_cardinality -
            (options.warm_history ? options.warm_history->tried_count() : 0);
        if (options.warm_history && (options.max_trials > remaining ||
            (options.candidate_policy == pfh::CandidatePolicy::Exhaustive &&
                options.max_trials != remaining)))
            throw pfh::detail::WarmStartError("finite budget does not fit remaining space");
        if (options.max_trials > *finite_cardinality) {
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "study"}},
                "finite candidate budget must not exceed search-space cardinality");
        }
        if (options.candidate_policy == pfh::CandidatePolicy::Exhaustive &&
            options.max_trials != remaining) {
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "study"}},
                options.warm_history
                    ? "exhaustive candidate budget must equal remaining search-space cardinality"
                    : "exhaustive candidate budget must equal search-space cardinality");
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
            throw pfh::TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "objective"}},
                "unknown report metric to record: " + name);
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
    configuration.symbol_feeds = options.symbol_feeds;
    if (!options.syminfo.empty())
        configuration.symbol_info = read_symbol_info(options.syminfo);
    const pfh::TrialExecutor executor(plugin, dataset, configuration);
    const auto bar_counts = pruner.bar_counts(dataset->size());
    pfh::EvaluationPolicy constraint_policy = options.evaluation_policy;
    constraint_policy.division_by_zero = pfh::DivisionByZeroPolicy::Reject;
    constraint_policy.non_finite_metric = pfh::NonFinitePolicy::Reject;
    constraint_policy.non_finite_result = pfh::NonFinitePolicy::Reject;

    TrialArchive archive(options, space, finite_cardinality.has_value());
    std::atomic<std::uint64_t> duplicate_proposals_skipped{0};
    RunState state(options, started, archive, [&](const std::vector<TrialRecord>& completed) {
        write_results(options, render_results(options, space, finite_cardinality, completed,
                                             best_trial(options, completed),
                                             duplicate_proposals_skipped.load(),
                                             "trial_timeout", &archive));
    });
    const auto direction = options.direction == Direction::kMaximize
                               ? pfh::ObjectiveDirection::Maximize
                               : pfh::ObjectiveDirection::Minimize;
    const auto batch_size = options.batch_size ? options.batch_size : options.workers;
    const auto available_ids = std::numeric_limits<std::uint64_t>::max() -
        (options.warm_history ? options.warm_history->next_id : 0);
    const auto worker_count = static_cast<unsigned>(std::min<std::uint64_t>(
        options.workers, options.max_trials ? options.max_trials :
            finite_cardinality.value_or(options.workers)));
    pfh::BatchExecutor<TrialRecord> workers(worker_count);
    double proposal_seconds = 0.0;
    double barrier_seconds = 0.0;
    const auto evaluate_batches = [&](auto propose, auto feedback) {
        std::deque<std::vector<std::future<TrialRecord>>> pending;
        std::uint64_t proposed = 0;
        bool exhausted = false;
        const auto submit_batch = [&] {
            if (state.stopped()) {
                exhausted = true;
                return;
            }
            std::vector<std::future<TrialRecord>> batch;
            const auto cuts = pruner.cuts();
            for (std::uint64_t index = 0; index < batch_size &&
                 (!options.max_trials || proposed < options.max_trials); ++index) {
                if (state.stopped() || proposed == available_ids) {
                    exhausted = true;
                    break;
                }
                const auto proposal_start = RunState::Clock::now();
                auto candidate = propose();
                proposal_seconds += std::chrono::duration<double>(
                    RunState::Clock::now() - proposal_start).count();
                if (!candidate) {
                    exhausted = true;
                    break;
                }
                ++proposed;
                batch.push_back(workers.submit([&, candidate = std::move(*candidate), cuts] {
                    if (!state.begin(candidate)) {
                        state.skip(candidate.id);
                        return make_trial_record(candidate, options);
                    }
                    auto trial = evaluate_candidate(candidate, space, options, executor, objective,
                                                    constraints, expressions, constraint_policy,
                                                    pruner, bar_counts, cuts);
                    state.finish(trial);
                    return trial;
                }));
            }
            if (!batch.empty())
                pending.push_back(std::move(batch));
            else
                exhausted = true;
        };
        while (!exhausted && pending.size() < options.batch_lag + 1)
            submit_batch();
        while (!pending.empty()) {
            auto batch = std::move(pending.front());
            pending.pop_front();
            std::vector<TrialRecord> completed;
            completed.reserve(batch.size());
            for (auto& result : batch) {
                const auto wait_start = RunState::Clock::now();
                while (result.wait_for(std::chrono::milliseconds(50)) !=
                       std::future_status::ready)
                    state.stopped();
                completed.push_back(result.get());
                barrier_seconds += std::chrono::duration<double>(
                    RunState::Clock::now() - wait_start).count();
            }
            for (auto& trial : completed) {
                feedback(trial);
                if (trial.status == "pending")
                    continue;
                pruner.observe(trial.rung_scores);
            }
            if (!exhausted)
                submit_batch();
        }
    };
    const auto evaluate_adaptive = [&](auto& sampler) {
        evaluate_batches([&] {
            auto candidate = sampler.ask();
            if constexpr (std::is_same_v<std::decay_t<decltype(sampler)>, pfh::TpeSampler>)
                duplicate_proposals_skipped.store(sampler.duplicate_proposals_skipped());
            return candidate;
        }, [&](const TrialRecord& trial) {
            if (trial.feasible && trial.objective && std::isfinite(*trial.objective))
                sampler.tell(trial.trial_id, *trial.objective);
            else
                sampler.abandon(trial.trial_id);
        });
    };

    if (options.sampler == "dlib_global") {
        pfh::DlibGlobalSampler sampler(space, options.seed, direction, options.max_trials);
        evaluate_adaptive(sampler);
    } else if (options.sampler == "tpe") {
        const auto policy = options.warm_history &&
            options.candidate_policy == pfh::CandidatePolicy::Exhaustive
            ? pfh::CandidatePolicy::WithoutReplacement : options.candidate_policy;
        pfh::TpeSampler sampler(space, options.seed, direction, options.max_trials,
                                options.tpe_config, policy);
        options.numeric_build_identity =
            pfh::detail::sampler_checkpoint_numeric_identity(sampler.sampler_state());
        if (options.warm_history) {
            const auto replay_batch = options.batch_lag == 0 ? batch_size : 0;
            const auto state = options.batch_lag == 0 ? options.warm_history->sampler_state :
                                                       std::string{};
            try {
                options.parent_numeric_build_identity =
                    pfh::detail::sampler_checkpoint_numeric_identity(state);
                options.tpe_warm_restored = options.warm_history->binary
                    ? sampler.warm_start(options.warm_history->binary, replay_batch, state)
                    : sampler.warm_start(options.warm_history->observations, replay_batch, state);
            } catch (const std::exception& error) {
                throw pfh::detail::WarmStartError(error.what());
            }
        }
        evaluate_adaptive(sampler);
        if (sampler.outstanding() == 0)
            options.tpe_sampler_state = sampler.sampler_state();
        duplicate_proposals_skipped.store(sampler.duplicate_proposals_skipped());
    } else {
        std::unique_ptr<pfh::Sampler> sampler;
        if (options.sampler == "grid")
            sampler = std::make_unique<pfh::GridSampler>(space);
        else
            sampler = std::make_unique<pfh::RandomSampler>(space,
                pfh::continuation_seed(options.seed, options.warm_history ?
                    options.warm_history->size() : 0),
                options.warm_history ? 0 : options.max_trials);
        std::uint64_t next_id = options.warm_history ? options.warm_history->next_id : 0;
        std::uint64_t fallback_ordinal = 0;
        evaluate_batches([&]() -> std::optional<pfh::Candidate> {
            for (std::uint64_t attempt = 0;; ++attempt) {
                auto candidate = sampler->next();
                if (!candidate)
                    return std::nullopt;
                if (options.warm_history && options.warm_history->contains(*candidate)) {
                    if (options.sampler == "random" && finite_cardinality && attempt >= 63) {
                        do {
                            if (fallback_ordinal >= *finite_cardinality)
                                return std::nullopt;
                            candidate = space.candidate_at(fallback_ordinal++, next_id);
                        } while (options.warm_history->contains(*candidate));
                    } else {
                        continue;
                    }
                }
                candidate->id = next_id++;
                return candidate;
            }
        }, [](const TrialRecord&) {});
    }
    workers.close();
    const double capacity = workers.elapsed_seconds() * worker_count;

    state.shutdown();
    if (archive.completed == 0 && !state.stopped())
        throw pfh::TypedHpoError<std::runtime_error>("hpo_invariant", {},
                                                     "sampler produced no candidates");

    const auto trials = archive.retained();
    const auto best_index = best_trial(options, trials);

    const auto render_start = RunState::Clock::now();
    const auto json = render_results(options, space, finite_cardinality, trials, best_index,
                                     duplicate_proposals_skipped.load(), state.stop_reason(),
                                     &archive);
    const auto write_start = RunState::Clock::now();
    state.check_error();
    write_result_file(options, json);
    const auto write_end = RunState::Clock::now();
    if (!options.scheduler_stats.empty()) {
        std::ofstream stats(options.scheduler_stats);
        if (!stats)
            throw pfh::TypedHpoError<std::runtime_error>("hpo_output_io_failed", {},
                                                         "cannot open scheduler stats file");
        stats << "{\"worker_seconds\":" << json_number(capacity)
              << ",\"busy_seconds\":" << json_number(workers.busy_seconds())
              << ",\"idle_seconds\":"
              << json_number(std::max(0.0, capacity - workers.busy_seconds()))
              << ",\"proposal_seconds\":" << json_number(proposal_seconds)
              << ",\"barrier_seconds\":" << json_number(barrier_seconds)
              << ",\"progress_serialization_seconds\":"
              << json_number(state.serialization_seconds())
              << ",\"progress_write_seconds\":" << json_number(state.progress_write_seconds())
              << ",\"progress_bytes\":" << state.progress_bytes()
              << ",\"final_json_bytes\":" << json.size()
              << ",\"final_json_render_seconds\":"
              << json_number(std::chrono::duration<double>(write_start - render_start).count())
              << ",\"final_json_write_seconds\":"
              << json_number(std::chrono::duration<double>(write_end - write_start).count())
              << "}\n";
        if (!stats)
            throw pfh::TypedHpoError<std::runtime_error>("hpo_output_io_failed", {},
                                                         "failed writing scheduler stats file");
    }
    publish_result(json);
    return best_index ? 0 : 2;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc > 1 && std::string(argv[1]) == "warm-encode") {
            std::filesystem::path spec;
            std::filesystem::path symbol_feeds;
            std::filesystem::path input;
            std::filesystem::path output;
            std::uint64_t block_trials = 0;
            for (int index = 2; index < argc; ++index) {
                const std::string option = argv[index];
                if (option == "--spec")
                    spec = require_value(argc, argv, index, option);
                else if (option == "--symbol-feeds")
                    symbol_feeds = require_value(argc, argv, index, option);
                else if (option == "--input")
                    input = require_value(argc, argv, index, option);
                else if (option == "--output")
                    output = require_value(argc, argv, index, option);
                else if (option == "--block-trials") {
                    const auto value = require_value(argc, argv, index, option);
                    if (value.empty() || value.find_first_not_of("0123456789") !=
                                             std::string::npos)
                        usage_error("--block-trials must be positive");
                    block_trials = std::stoull(value);
                    if (!block_trials)
                        usage_error("--block-trials must be positive");
                }
                else
                    usage_error("unknown warm-encode option: " + option);
            }
            if (spec.empty() || input.empty() || output.empty())
                usage_error("warm-encode requires --spec, --input and --output");
            const auto recorded = pfh::detail::space_with_symbol_feeds(spec, symbol_feeds);
            const auto space = pfh::detail::search_space_from_recorded(recorded);
            const auto history = pfh::detail::load_warm_history(input, space, recorded);
            pfh::detail::encode_warm_history(output, space, recorded, history, block_trials);
            return 0;
        }
        if (argc > 1 && std::string(argv[1]) == "space-info") {
            std::filesystem::path spec;
            std::filesystem::path symbol_feeds;
            std::filesystem::path warm;
            bool warm_details = false;
            bool warm_digest = false;
            bool defer_symbol_feeds = false;
            for (int index = 2; index < argc; ++index) {
                const std::string option = argv[index];
                if (option == "--spec")
                    spec = require_value(argc, argv, index, option);
                else if (option == "--symbol-feeds")
                    symbol_feeds = require_value(argc, argv, index, option);
                else if (option == "--warm-start")
                    warm = require_value(argc, argv, index, option);
                else if (option == "--warm-details")
                    warm_details = true;
                else if (option == "--warm-digest")
                    warm_digest = true;
                else if (option == "--defer-symbol-feeds")
                    defer_symbol_feeds = true;
                else
                    usage_error("unknown space-info option: " + option);
            }
            if (spec.empty())
                usage_error("space-info requires --spec");
            defer_symbol_feeds = defer_symbol_feeds && symbol_feeds.empty();
            auto recorded = defer_symbol_feeds ? pfh::detail::space_from_spec(
                pfh::detail::parse_json(pfh::detail::read_document(spec), 256 * 1024 * 1024)) :
                pfh::detail::space_with_symbol_feeds(spec, symbol_feeds);
            if (defer_symbol_feeds)
                recorded.members["_defer_symbol_feeds"] = pfh::detail::Json::boolean(true);
            const auto space = pfh::detail::search_space_from_recorded(recorded);
            std::optional<std::uint64_t> cardinality;
            try {
                cardinality = space.finite_cardinality();
            } catch (const std::overflow_error&) {
            }
            const auto history = pfh::detail::load_warm_history(warm, space, recorded);
            const auto tried = history.tried_count();
            std::cout << "{\"cardinality\":"
                      << (cardinality ? std::to_string(*cardinality) : "null")
                      << ",\"tried\":" << tried << ",\"remaining\":"
                      << (cardinality ? std::to_string(*cardinality - tried) : "null")
                      << ",\"space_hash\":\"" << pfh::detail::space_hash(recorded) << "\"";
            if (warm_details)
                std::cout << ",\"warm_trials\":" << history.size() << ",\"next_id\":"
                          << history.next_id << ",\"completed\":" << history.completed
                          << ",\"feasible\":" << history.feasible;
            if (warm_digest)
                std::cout << ",\"source_sha256\":\"" << history.source_digest() << "\"";
            std::cout << "}\n";
            return 0;
        }
        struct sigaction action {};
        action.sa_handler = request_stop;
        action.sa_flags = SA_RESTART;
        sigemptyset(&action.sa_mask);
        if (::sigaction(SIGTERM, &action, nullptr) != 0 ||
            ::sigaction(SIGINT, &action, nullptr) != 0)
            throw pfh::TypedHpoError<std::runtime_error>(
                "hpo_invariant", {}, "cannot install cooperative stop handlers");
        action.sa_handler = SIG_IGN;
        if (::sigaction(SIGPIPE, &action, nullptr) != 0)
            throw pfh::TypedHpoError<std::runtime_error>(
                "hpo_invariant", {}, "cannot install progress I/O error handler");
        return run(parse_options(argc, argv));
    } catch (const pfh::detail::WarmStartError& error) {
        std::cerr << "pineforge-hpo-native: " << error.what() << '\n';
        std::cout << pfh::detail::failure_document(error, 4);
        return 4;
    } catch (const pfh::detail::SpaceExhausted& error) {
        std::cerr << "pineforge-hpo-native: " << error.what() << '\n';
        std::cout << pfh::detail::failure_document(error, 5);
        return 5;
    } catch (const std::exception& error) {
        std::cerr << "pineforge-hpo-native: " << error.what() << '\n';
        std::cout << pfh::detail::failure_document(error, 1);
        return 1;
    }
}
