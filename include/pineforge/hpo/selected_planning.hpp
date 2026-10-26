#pragma once

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/selected_evaluation.hpp>
#include <pineforge/hpo/window_plan.hpp>

#include <cstdint>

namespace pineforge::hpo {

/// One selected-window prediction and the immutable rows to supply to SelectedEvaluation.
///
/// The six expected counts omit trimmed_script_bars, available_script_bars, shortfall and
/// complete_pending_preroll_at_horizon; those facts remain available in plan.raw. A pre-roll
/// shortfall is returned for caller policy and is not a planning error.
struct SelectedPlan {
    /// Complete engine result, including trimming, optional timestamps and shortfall flags.
    PlanResult plan;
    /// Tail of the bounded [F, E) input, starting at the planner's trim_index; retains its owner.
    DatasetView retained;
    /// Execution expectations; preroll_script_bars maps to the planner's used_script_bars.
    SelectedExpectedCounts expected;
    /// Owner-relative index of the first supplied row, before applying trim_index.
    std::uint64_t supplied_first_index;
};

/// Plans one window over a shared tape or prefix without copying or repacking any bars.
///
/// Binary searches cut the input to F <= timestamp < E before calling the helper. Dataset
/// construction and both loaders already enforce strictly increasing timestamps; the owner
/// must remain immutable, including mapped bytes. The request's T/E/F/N, timeframes and clock
/// are passed unchanged. Empty cuts (including a possibly null pointer with count zero) reach
/// the planner unchanged. Its retained rows are exactly the tail after trim_index.
///
/// Cutting and checking row endpoints take O(log n) time and constant auxiliary storage; the
/// engine planner walks the bounded rows. No allocation scales with the tape. The returned
/// view keeps the Dataset alive. Concurrent calls are compatible with an immutable tape and
/// a helper kept alive for every call. Mapped-file verification remains the owner's duty.
///
/// @param helper Already loaded, verified planner helper.
/// @param tape Owner-retaining immutable primary input, possibly spanning several folds.
/// @param request One unmodified engine planning request.
/// @return Complete prediction, retained view and six native execution expectations.
/// @throws TypedHpoError<std::invalid_argument> with hpo_walkforward_plan_refused and closed
///         status/option arguments for a planner refusal.
/// @throws HpoError with hpo_invariant for inconsistent successful plan data. Helper bridge
///         failures propagate unchanged, including reason window_plan_load.
SelectedPlan plan_selected_window(const WindowPlanHelper& helper, const DatasetView& tape,
                                  const PlanRequest& request);

namespace detail {

/// Internal test seam for raw-result validation and refusal conversion; not a planning API.
struct SelectedPlanningTestAccess {
    /// Checks successful result identities, counts, masks and row endpoints without a helper.
    /// @param plan Hand-built raw result to validate; its descriptor header is not rechecked.
    /// @param bounded Already bounded [F, E) input view.
    /// @param request Unmodified request used to derive the plan.
    /// @throws HpoError with hpo_invariant, naming the inconsistent field.
    static void validate(const PlanResult& plan, const DatasetView& bounded,
                         const PlanRequest& request);

    /// Applies the production refusal mapping to a real-helper or hand-built result.
    /// @param plan Result with a valid helper descriptor.
    /// @param request Unmodified request, used only for diagnostic values.
    /// Unknown status words (> 6) reach invariant("status", ...) only through this seam;
    /// the helper already rejects them.
    /// @throws HpoError for a planner refusal or an unknown status/option.
    static void check_status(const PlanResult& plan, const PlanRequest& request);
};

}  // namespace detail
}  // namespace pineforge::hpo
