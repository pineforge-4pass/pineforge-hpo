#pragma once

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/objective.hpp>
#include <pineforge/hpo/pruner.hpp>
#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>
#include <pineforge/hpo/trial_executor.hpp>

#include "candidate_list.hpp"
#include "continuation.hpp"
#include "sobol_continuation.hpp"

#include <chrono>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace pineforge::hpo::detail {
struct ResultDocument;
}

namespace pineforge::hpo::cli {

enum class Direction { kMaximize, kMinimize };

#ifdef PINEFORGE_HPO_PROOF_CURVES
class ProofCurveWriter;
#endif

// Single-TU rule: every translation unit that sees Options must include this header with the
// same PINEFORGE_HPO_PROOF_CURVES setting.
struct Options {
    std::filesystem::path strategy;
    std::filesystem::path ohlcv;
    std::filesystem::path ohlcv_bin;
    BinaryVerification verify_bin = BinaryVerification::Full;
    std::filesystem::path output;
    std::filesystem::path trials_file;
    std::filesystem::path warm_start;
    std::shared_ptr<const detail::WarmHistory> warm_history;
    std::filesystem::path candidates;
    std::shared_ptr<const detail::CandidateList> candidate_list;
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
    std::shared_ptr<const SymbolFeeds> symbol_feeds;
    std::optional<detail::Json> symbol_feeds_record;
    std::string objective;
    std::vector<std::string> constraints;
    std::vector<std::string> recorded_metrics;
    std::vector<Dimension> dimensions;
    std::map<std::string, std::vector<ParameterValue>> categorical_choices;
    std::vector<std::string> categorical_order;
    ParameterValues fixed_inputs;
    ParameterValues strategy_overrides;
    std::string sampler = "grid";
    CandidatePolicy candidate_policy = CandidatePolicy::SamplerDefault;
    std::string artifact_key;
    std::string input_timeframe;
    std::string script_timeframe;
    std::string chart_timezone;
    std::uint64_t seed = 0;
    std::uint64_t max_trials = 0;
    // Presence of --max-trials, kept apart from the zero that means "not given" elsewhere.
    bool max_trials_given = false;
    // Sobol sampler: the scramble spelling, the run's descriptor (identity, columns, numeric
    // identity), the part's first index (the parent's next ID, 0 when fresh) and whether the
    // admitted parent holds exactly the IDs 0..first_index-1.
    std::string sobol_scramble = "digital_shift";
    bool sobol_scramble_given = false;
    std::shared_ptr<const detail::SobolDescriptor> sobol_descriptor;
    std::uint64_t sobol_first_index = 0;
    bool sobol_parent_exact = true;
    // Return-statistics series named by an objective, constraint or recorded metric.
    bool return_stats_bar = false;
    bool return_stats_monthly = false;
    std::uint32_t pbo_blocks = 0;
#ifdef PINEFORGE_HPO_PROOF_CURVES
    std::filesystem::path proof_curves;
    std::shared_ptr<ProofCurveWriter> proof_curve_writer;
#endif
    std::uint64_t no_improvement_trials = 0;
    unsigned workers = 1;
    int progress_fd = -1;
    double max_wall_seconds = 0.0;
    double wall_grace_seconds = 30.0;
    double trial_timeout_seconds = 0.0;
    std::uint64_t batch_size = 0;
    unsigned batch_lag = 0;
    // execution.schedule, forwarded as --schedule. "batches" is the default and keeps the bytes of
    // 0.12.0; "rolling" refills a free worker slot at once. The *_given flags record that a flag
    // was present, so --schedule rolling can refuse an explicit --batch-size or --batch-lag even
    // when its value is the default (presence, not value).
    std::string schedule = "batches";
    bool batch_size_given = false;
    bool batch_lag_given = false;
    std::filesystem::path scheduler_stats;
    PrunerKind pruner = PrunerKind::None;
    std::string pruner_name = "none";
    std::vector<double> pruner_rungs{0.25, 0.5};
    unsigned pruner_eta = 2;
    bool bar_magnifier = false;
    int magnifier_samples = 4;
    pf_magnifier_distribution_t magnifier_distribution = PF_MAGNIFIER_ENDPOINTS;
    Direction direction = Direction::kMaximize;
    EvaluationPolicy evaluation_policy;
    TpeSamplerConfig tpe_config;
};

// Admission is performed once. No sampler, archive, worker pool or spool lives here. The pruner
// is the admitted initial history (including warm-start rungs), copied into each study.
// The proof-curve writer and --trials-file destination are shared across sequential run_study
// calls; they must become per-study before walk-forward.
struct PreparedStudy {
    Options options;
    SearchSpace space;
    std::optional<std::uint64_t> finite_cardinality;
    Pruner pruner;
    MetricExpression objective;
    std::vector<MetricExpression> constraints;
    std::vector<MetricExpression> recorded_metrics;
    std::shared_ptr<const StrategyPlugin> plugin;
    std::shared_ptr<const Dataset> dataset;
    BacktestConfiguration configuration;
};

using StudyClock = std::chrono::steady_clock;

struct StudyStatistics {
    double worker_seconds = 0.0;
    double busy_seconds = 0.0;
    double proposal_seconds = 0.0;
    double barrier_seconds = 0.0;
    double progress_serialization_seconds = 0.0;
    double progress_write_seconds = 0.0;
    std::uint64_t progress_bytes = 0;
    StudyClock::time_point render_started;
};

// Publication is synchronous while the study owns its sealed spool. A callback must consume
// the borrowed ResultDocument before returning and may not retain it. The result below contains
// only owned data. Cancellation, input verification and process termination are outer concerns;
// the ordinary CLI supplies its existing signal latch, owner guard and timeout exit handlers.
struct StudyCallbacks {
    // May be queried by workers and the progress writer; the supplied callable must be thread-safe.
    std::function<bool()> cancelled;
    std::function<void()> before_publish;
    std::function<void(const detail::ResultDocument&, const StudyStatistics&)> publish;
    std::function<void(const detail::ResultDocument&)> timeout_publish;
    // Both timeout handlers must terminate the outer run and must not return.
    std::function<void(std::exception_ptr)> timeout_exit;
    std::function<void(const std::exception&)> timeout_failure;
};

struct StudyResult {
    std::optional<Candidate> best_candidate;
    std::optional<double> best_value;
    std::uint64_t trials_completed = 0;
    std::exception_ptr progress_failure;
    int exit_code = 2;
};

PreparedStudy prepare(Options options);

// Each invocation owns and destroys its sampler, pruner history, archive, spool, progress writer
// and workers. The dataset/plugin in prepare are reused; a view may also retain another admitted
// Dataset with the same execution configuration. No process-global latch is read or reset here.
StudyResult run_study(const PreparedStudy& prepared, DatasetView view,
                      const StudyCallbacks& callbacks = {},
                      StudyClock::time_point started = StudyClock::now());

}  // namespace pineforge::hpo::cli
