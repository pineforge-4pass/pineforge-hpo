#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/selected_planning.hpp>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <stdexcept>
#include <string>

namespace pineforge::hpo {
namespace {

[[noreturn]] void invariant(const std::string& field, const char* rule) {
    throw TypedHpoError<>("hpo_invariant", {},
                          "selected planning: " + field + " " + rule);
}

void require(bool condition, const char* field, const char* rule) {
    if (!condition)
        invariant(field, rule);
}

// Index arithmetic avoids even zero-offset arithmetic on a null empty-tape pointer.
std::size_t lower_timestamp(const DatasetView& view, std::int64_t timestamp,
                            std::size_t first = 0) {
    std::size_t last = view.size();
    while (first < last) {
        const auto middle = first + (last - first) / 2;
        if (view.data()[middle].timestamp < timestamp)
            first = middle + 1;
        else
            last = middle;
    }
    return first;
}

const char* refusal_status(PlanStatus status) {
    switch (status) {
    case PlanStatus::RequestInvalid: return "request_invalid";
    case PlanStatus::CalendarUnsupported: return "calendar_unsupported";
    case PlanStatus::BoundaryUnaligned: return "boundary_unaligned";
    case PlanStatus::FeedRangeInvalid: return "feed_range_invalid";
    case PlanStatus::RowsUnordered: return "rows_unordered";
    case PlanStatus::InternalError: return "internal_error";
    case PlanStatus::Ok: break;
    }
    invariant("status", "is not a known planner refusal");
}

void check_plan_status(const PlanResult& plan, const PlanRequest& request) {
    if (plan.ok())
        return;
    const auto& raw = plan.raw;
    const char* const status = refusal_status(plan.status());
    const auto end = std::find(std::begin(raw.option), std::end(raw.option), '\0');
    require(end != std::end(raw.option), "option", "is not terminated");
    const std::string option = end == std::begin(raw.option) ? "none" :
        std::string(std::begin(raw.option), end);
    // Entire refusal vocabulary in engine selected_window_plan.hpp:43-70, plus empty -> none.
    constexpr const char* options[]{
        "calendar", "end_ms", "engine_timezone", "fed_start_ms", "input_tf", "none",
        "preroll_bars", "rows", "script_tf", "session", "start_ms", "timeframes", "timezone"};
    require(std::any_of(std::begin(options), std::end(options),
                        [&](const char* name) { return option == name; }),
            "option", "is outside the documented planner refusal vocabulary");
    const char* boundary = "none";
    switch (raw.bound) {
    case -1: break;
    case 0: boundary = "T"; break;
    case 1: boundary = "E"; break;
    case 2: boundary = "F"; break;
    default: invariant("bound", "is outside -1/0/1/2");
    }
    std::string message = "Selected-window plan refused (" + std::string(status) + ": " +
        option + "): value_ms=" + std::to_string(raw.value_ms) +
        ", bound=" + std::to_string(raw.bound) + " (" + boundary +
        "), previous_boundary_ms=" + std::to_string(raw.previous_boundary_ms) +
        ", next_boundary_ms=" + std::to_string(raw.next_boundary_ms) + ".";
    if (option == "timeframes" || option == "input_tf" || option == "script_tf")
        message += " input_tf='" + request.input_tf + "', script_tf='" + request.script_tf + "'.";
    if (option == "timezone" || option == "engine_timezone" || option == "session")
        message += " chart_timezone='" + request.chart_timezone + "', engine_timezone='" +
            request.engine_timezone + "', session='" + request.session + "'.";
    throw TypedHpoError<std::invalid_argument>(
        "hpo_walkforward_plan_refused", {{"status", status}, {"option", option}}, message);
}

struct TimestampGroup {
    unsigned first_bit;
    std::uint64_t count;
    std::int64_t first;
    std::int64_t last;
    const char* first_name;
    const char* last_name;
};

void validate_timestamps(const pf_selected_plan_result_v1& raw, const PlanRequest& request) {
    // C descriptor bits follow these eight fields (selected_window_plan.h:70-77).
    // Pre-roll fields are SCRIPT OPEN LABELS; the other three pairs are INPUT ROW timestamps.
    // A window with rows but an unsealed tail still has window timestamps and zero script bars.
    const TimestampGroup groups[]{
        {0, raw.used_script_bars, raw.preroll_first_bar_ms, raw.preroll_last_bar_ms,
         "preroll_first_bar_ms", "preroll_last_bar_ms"},
        {2, raw.supplied_input_bars, raw.supplied_first_data_ms, raw.supplied_last_data_ms,
         "supplied_first_data_ms", "supplied_last_data_ms"},
        {4, raw.fed_input_bars, raw.fed_first_data_ms, raw.fed_last_data_ms,
         "fed_first_data_ms", "fed_last_data_ms"},
        {6, raw.window_input_bars, raw.window_first_data_ms, raw.window_last_data_ms,
         "window_first_data_ms", "window_last_data_ms"}};
    for (const auto& group : groups) {
        const auto first_mask = std::uint32_t{1} << group.first_bit;
        const auto last_mask = first_mask << 1;
        require(((raw.present_mask & first_mask) != 0) == (group.count != 0),
                group.first_name, "present_mask disagrees with its count");
        require(((raw.present_mask & last_mask) != 0) == (group.count != 0),
                group.last_name, "present_mask disagrees with its count");
        if (group.count == 0) {
            // Also checked by WindowPlanHelper; retain the check for this raw-result seam.
            require(group.first == 0, group.first_name, "is nonzero while absent");
            require(group.last == 0, group.last_name, "is nonzero while absent");
            continue;
        }
        require(group.first <= group.last, group.first_name, "is after its last timestamp");
        require(group.first >= request.fed_start_ms, group.first_name, "is below F");
        require(group.last < request.end_ms, group.last_name, "is at or after E");
    }
    if (raw.used_script_bars != 0)
        require(raw.preroll_last_bar_ms < request.start_ms,
                "preroll_last_bar_ms", "is at or after T");
    if (raw.window_input_bars != 0) {
        require(raw.window_first_data_ms >= request.start_ms,
                "window_first_data_ms", "is below T");
        require(raw.window_first_data_ms >= raw.fed_first_data_ms,
                "window_first_data_ms", "is before fed_first_data_ms");
        require(raw.window_last_data_ms <= raw.fed_last_data_ms,
                "window_last_data_ms", "is after fed_last_data_ms");
    }
}

void validate_plan(const PlanResult& plan, const DatasetView& bounded,
                   const PlanRequest& request) {
    const auto& raw = plan.raw;
    require(plan.ok(), "status", "must be Ok for plan validation");
    // The helper already checks header/version/reserved, status/bound/mask encoding, option
    // encoding and zero absent timestamps. Do not duplicate its descriptor admission here.
    // These two Boolean checks are deliberately repeated for the raw-result test seam.
    require(raw.shortfall <= 1, "shortfall", "must be exactly 0 or 1");
    require(raw.complete_pending_preroll_at_horizon <= 1,
            "complete_pending_preroll_at_horizon", "must be exactly 0 or 1");
    static_assert(sizeof(std::size_t) <= sizeof(std::uint64_t), "view size must fit planner ABI");
    require(raw.supplied_input_bars == bounded.size(),
            "supplied_input_bars", "differs from bounded.size()");
    require(raw.trim_index <= raw.supplied_input_bars,
            "trim_index", "exceeds supplied_input_bars");
    // Engine selected_window_plan.hpp:96-109 gives the fields; the C contract at .h:67-68
    // carries their equalities. The exact assignments are selected_window_plan.cpp:403-413
    // and the wire checks are docker/selected_window_report.py:486-493.
    // Subtraction after a bound check cannot wrap, unlike summing untrusted counts.
    require(raw.fed_input_bars == raw.supplied_input_bars - raw.trim_index,
            "fed_input_bars", "differs from supplied_input_bars - trim_index");
    require(raw.preroll_input_bars <= raw.fed_input_bars,
            "preroll_input_bars", "exceeds fed_input_bars");
    require(raw.window_input_bars == raw.fed_input_bars - raw.preroll_input_bars,
            "window_input_bars", "differs from fed_input_bars - preroll_input_bars");
    require(raw.used_script_bars <= raw.fed_script_bars,
            "used_script_bars", "exceeds fed_script_bars");
    require(raw.window_script_bars == raw.fed_script_bars - raw.used_script_bars,
            "window_script_bars", "differs from fed_script_bars - used_script_bars");
    require(raw.available_script_bars <= raw.supplied_script_bars,
            "available_script_bars", "exceeds supplied_script_bars");
    require(raw.window_script_bars == raw.supplied_script_bars - raw.available_script_bars,
            "supplied_script_bars", "differs from available_script_bars + window_script_bars");
    require(raw.used_script_bars ==
                std::min<std::uint64_t>(request.preroll_bars, raw.available_script_bars),
            "used_script_bars", "differs from min(preroll_bars, available_script_bars)");
    require(raw.trimmed_script_bars == raw.available_script_bars - raw.used_script_bars,
            "trimmed_script_bars", "differs from available_script_bars - used_script_bars");
    require(raw.shortfall == (raw.available_script_bars < request.preroll_bars ? 1u : 0u),
            "shortfall", "disagrees with available_script_bars < preroll_bars");
    require(raw.fed_script_bars <= raw.fed_input_bars,
            "fed_script_bars", "exceeds retained input rows");

    validate_timestamps(raw, request);
    // These are input ROW endpoints, not the pre-roll group's potentially earlier open label.
    // Engine selected_window_plan.cpp:419-429 assigns them from these exact supplied indices.
    const auto window_begin = lower_timestamp(bounded, request.start_ms);
    require(raw.trim_index <= window_begin, "trim_index", "discards selected window rows");
    require(raw.window_input_bars == bounded.size() - window_begin,
            "window_input_bars", "differs from the number of rows at or after T");
    if (!bounded.empty()) {
        require(raw.supplied_first_data_ms == bounded.data()[0].timestamp,
                "supplied_first_data_ms", "differs from the first supplied row");
        require(raw.supplied_last_data_ms == bounded.data()[bounded.size() - 1].timestamp,
                "supplied_last_data_ms", "differs from the last supplied row");
    }
    if (raw.fed_input_bars != 0) {
        require(raw.fed_first_data_ms ==
                    bounded.data()[static_cast<std::size_t>(raw.trim_index)].timestamp,
                "fed_first_data_ms", "differs from the row at trim_index");
        require(raw.fed_last_data_ms == bounded.data()[bounded.size() - 1].timestamp,
                "fed_last_data_ms", "differs from the last supplied row");
    }
    if (raw.window_input_bars != 0) {
        require(raw.window_first_data_ms == bounded.data()[window_begin].timestamp,
                "window_first_data_ms", "differs from the first row at or after T");
        require(raw.window_last_data_ms == bounded.data()[bounded.size() - 1].timestamp,
                "window_last_data_ms", "differs from the last supplied row");
    }
    require(raw.trim_start_ms ==
                (raw.used_script_bars != 0 ? raw.preroll_first_bar_ms : request.start_ms),
            "trim_start_ms", "differs from the first kept script open or T");
}

}  // namespace

SelectedPlan plan_selected_window(const WindowPlanHelper& helper, const DatasetView& tape,
                                  const PlanRequest& request) {
    // Dataset constructor: dataset.cpp:205-208; mapped load: dataset.cpp:449-452.
    // Both enforce strict ordering before a DatasetView can be constructed.
    const auto first = lower_timestamp(tape, request.fed_start_ms);
    // Searching the suffix also leaves an empty cut for F >= E; the unchanged request then
    // reaches the engine's request admission rather than underflowing last - first.
    const auto last = lower_timestamp(tape, request.end_ms, first);
    // first <= tape.length <= owner->size() - tape.begin (DatasetView's constructor).
    // Thus adding this checked offset cannot overflow, including at the empty tail.
    const DatasetView bounded(tape.owner, tape.begin + first, last - first);
    const auto plan = helper.plan_selected_primary(bounded.data(), bounded.size(), request);
    check_plan_status(plan, request);
    validate_plan(plan, bounded, request);
    const auto& raw = plan.raw;
    // Validation proves trim_index <= bounded.size() and fed == size - trim, hence the
    // retained tail ends exactly at bounded's end. Both casts and this addition are bounded.
    const DatasetView retained(bounded.owner,
                               bounded.begin + static_cast<std::size_t>(raw.trim_index),
                               static_cast<std::size_t>(raw.fed_input_bars));
    const SelectedExpectedCounts expected{
        raw.fed_input_bars, raw.fed_script_bars, raw.preroll_input_bars,
        raw.used_script_bars, raw.window_input_bars, raw.window_script_bars};
    return {plan, retained, expected, static_cast<std::uint64_t>(bounded.begin)};
}

void detail::SelectedPlanningTestAccess::validate(const PlanResult& plan,
                                                 const DatasetView& bounded,
                                                 const PlanRequest& request) {
    validate_plan(plan, bounded, request);
}

void detail::SelectedPlanningTestAccess::check_status(const PlanResult& plan,
                                                     const PlanRequest& request) {
    check_plan_status(plan, request);
}

}  // namespace pineforge::hpo
