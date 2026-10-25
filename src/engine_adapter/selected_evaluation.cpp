#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/selected_evaluation.hpp>

#include <exception>
#include <limits>
#include <string>
#include <utility>

namespace pineforge {
namespace hpo {
namespace {

[[noreturn]] void invariant(const std::string& message) {
    throw TypedHpoError<>("hpo_invariant", {}, "selected evaluation: " + message);
}

struct BoundaryContext {
    std::uint32_t successful_calls = 0;
    pf_execution_boundary_v1 boundary{};
    std::exception_ptr failure;
};

int before_results(void* opaque, const pf_execution_boundary_v1* boundary,
                   pf_boundary_receipt_v1* receipt) noexcept {
    auto* const context = static_cast<BoundaryContext*>(opaque);
    // The registered context is always nonnull. Do not let any exception escape the C callback.
    if (!context)
        return -1;
    try {
        if (context->failure)
            return -1;
        if (context->successful_calls != 0)
            invariant("duplicate before-results callback");
        if (!boundary || !receipt || boundary->struct_size != sizeof(*boundary) ||
            boundary->version != 1 || boundary->run_generation == 0 ||
            boundary->attempt_serial == 0 || receipt->struct_size != sizeof(*receipt) ||
            receipt->version != 1 || receipt->run_generation != boundary->run_generation ||
            receipt->frame_bytes != UINT32_MAX || receipt->handed_bytes != 0 ||
            receipt->export_requested != 0 || receipt->reserved != 0)
            invariant("invalid before-results boundary or receipt");
        context->boundary = *boundary;
        receipt->frame_bytes = 0;
        receipt->handed_bytes = 0;
        receipt->export_requested = 0;
        receipt->reserved = 0;
        ++context->successful_calls;
        return 0;
    } catch (...) {
        if (!context->failure)
            context->failure = std::current_exception();
        return -1;
    }
}

// Declared after TrialResources, so removal/clear happen before report/handle destruction.
// BoundaryContext is declared before both, and survives handle destruction even if removal fails.
class SelectedCleanup final {
public:
    SelectedCleanup(const StrategyPlugin& plugin, pf_strategy_t strategy)
        : plugin_(plugin), strategy_(strategy) {}
    ~SelectedCleanup() { clean(); }

    void finish() {
        clean();
        if (failure_)
            std::rethrow_exception(failure_);
    }

private:
    void clean() noexcept {
        if (cleaned_)
            return;
        cleaned_ = true;
        try {
            plugin_.set_execution_observer(strategy_, nullptr);
        } catch (...) {
            failure_ = std::current_exception();
        }
        try {
            plugin_.clear_selected_window(strategy_);
        } catch (...) {
            if (!failure_)
                failure_ = std::current_exception();
        }
    }

    const StrategyPlugin& plugin_;
    pf_strategy_t strategy_;
    bool cleaned_ = false;
    std::exception_ptr failure_;
};

void validate_request(const pf_bar_t* bars, std::size_t count, std::int64_t start_ms,
                      std::int64_t end_ms, const SelectedExpectedCounts& expected,
                      const BacktestConfiguration& configuration) {
    constexpr std::int64_t json_safe = 9'007'199'254'740'991LL;
    if (!bars || count == 0 || count > static_cast<std::size_t>(std::numeric_limits<int>::max()))
        invariant("invalid retained input pointer/count");
    if (start_ms >= end_ms || start_ms < -json_safe || end_ms > json_safe)
        invariant("invalid planned selected bounds");
    if (expected.fed_input_bars != count ||
        expected.preroll_input_bars > expected.fed_input_bars ||
        expected.window_input_bars != expected.fed_input_bars - expected.preroll_input_bars ||
        expected.preroll_script_bars > expected.fed_script_bars ||
        expected.window_script_bars != expected.fed_script_bars - expected.preroll_script_bars ||
        expected.fed_script_bars > count ||
        expected.window_script_bars >=
            static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        invariant("inconsistent planned selected counts");
    if (configuration.magnifier_samples <= 0)
        invariant("magnifier_samples must be positive");
    switch (configuration.magnifier_distribution) {
    case PF_MAGNIFIER_UNIFORM:
    case PF_MAGNIFIER_COSINE:
    case PF_MAGNIFIER_TRIANGLE:
    case PF_MAGNIFIER_ENDPOINTS:
    case PF_MAGNIFIER_FRONT_LOADED:
    case PF_MAGNIFIER_BACK_LOADED:
        break;
    default:
        invariant("invalid magnifier distribution");
    }
    if (configuration.return_stats_bar || configuration.return_stats_monthly ||
        configuration.pbo_blocks != 0) {
        require_return_stats_identity();
        if (!return_stats_contraction_free())
            invariant("return statistics need a build without floating-point contraction");
    }
}

void check_result(const pf_selected_window_counts_v1& counts,
                  const pf_execution_observation_v1& observation,
                  const pf_execution_boundary_v1& boundary, const SelectedExpectedCounts& expected,
                  const pf_report_t& report) {
    if (!counts.run_generation || !counts.attempt_serial || !counts.attempt_generation ||
        counts.run_generation != observation.run_generation ||
        counts.attempt_serial != observation.attempt_serial ||
        counts.attempt_generation != observation.attempt_generation)
        invariant("selected counts and execution observation identities differ or are zero");
    if (counts.fed_input_bars != expected.fed_input_bars ||
        counts.fed_script_bars != expected.fed_script_bars ||
        counts.preroll_input_bars != expected.preroll_input_bars ||
        counts.preroll_script_bars != expected.preroll_script_bars ||
        counts.window_input_bars != expected.window_input_bars ||
        counts.window_script_bars != expected.window_script_bars)
        invariant("native selected counts differ from the plan");
    // The state words are the engine header's Results / None / ResultsOpen values.
    if (observation.phase != 4 || observation.fault_stage != 0 ||
        observation.attempt_outcome != 4 || observation.boundary_delivered != 1)
        invariant("observation is not fault-free Results/ResultsOpen with a delivered boundary");
    if (boundary.run_generation != observation.run_generation ||
        boundary.attempt_serial != observation.attempt_serial)
        invariant("callback boundary differs from the execution observation");
    if (report.input_bars_processed < 0 || report.script_bars_processed < 0 ||
        static_cast<std::uint64_t>(report.input_bars_processed) != expected.fed_input_bars ||
        static_cast<std::uint64_t>(report.script_bars_processed) != expected.fed_script_bars)
        invariant("report processed counts differ from planned fed counts");
    if (report.equity_curve_len < 0 ||
        static_cast<std::uint64_t>(report.equity_curve_len) != expected.window_script_bars + 1)
        invariant("selected equity curve length differs from window_script_bars + 1");
}

}  // namespace

SelectedEvaluationResult SelectedEvaluation::evaluate(
    const StrategyPlugin& plugin, const pf_bar_t* bars, std::size_t count,
    std::int64_t start_ms, std::int64_t end_ms, const SelectedExpectedCounts& expected,
    const ParameterValues& inputs, const ParameterValues& strategy_overrides,
    const BacktestConfiguration& configuration,
    const std::optional<std::string>& expected_manifest_version) {
    plugin.require_selected_window(expected_manifest_version);
    validate_request(bars, count, start_ms, end_ms, expected, configuration);

    BoundaryContext context;
    detail::TrialResources resources(plugin, plugin.create_strategy());
    SelectedCleanup cleanup(plugin, resources.strategy());
    detail::apply_trial_settings(plugin, resources.strategy(), inputs, strategy_overrides,
                                 configuration);
    // Legacy metadata setters can latch failures; do not let execution reset those errors.
    const auto check_engine = [&] {
        const auto error = plugin.last_error(resources.strategy());
        const auto code = plugin.last_error_code(resources.strategy());
        const auto status = plugin.last_run_status(resources.strategy());
        if (!error.empty() || (code && !code->empty()) || status == 1)
            throw EngineError(code && !code->empty() ? code : std::nullopt,
                              plugin.last_error_args(resources.strategy()), error);
        if (status && *status != 0)
            invariant("unknown engine run status " + std::to_string(*status));
    };
    check_engine();
    auto settings = plugin.effective_settings(resources.strategy());
    check_engine();
    plugin.set_selected_window(resources.strategy(), start_ms, end_ms);
    const pf_execution_observer_v1 observer{sizeof(pf_execution_observer_v1), 1, &context,
                                           before_results};
    plugin.set_execution_observer(resources.strategy(), &observer);
    plugin.run_backtest_full(resources.strategy(), bars, static_cast<int>(count),
                             configuration.input_timeframe, configuration.script_timeframe,
                             configuration.bar_magnifier, configuration.magnifier_samples,
                             configuration.magnifier_distribution, &resources.report);
    if (context.failure)
        std::rethrow_exception(context.failure);
    check_engine();
    if (context.successful_calls != 1)
        invariant("expected exactly one successful before-results callback");
    const auto observation = plugin.execution_observation(resources.strategy());
    const auto counts = plugin.selected_window_counts(resources.strategy());
    check_result(counts, observation, context.boundary, expected, resources.report);

    auto capture = configuration;
    capture.capture_equity_curve = true;
    capture.capture_trades = true;
    SelectedEvaluationResult result;
    result.counts = counts;
    result.observation = observation;
    result.boundary = context.boundary;
    result.effective_settings = std::move(settings);
    result.report = detail::copy_report(resources.report, capture, capture.pbo_blocks != 0);
    cleanup.finish();
    return result;
}

}  // namespace hpo
}  // namespace pineforge
