#pragma once

#include <cmath>
#include <cstdint>
#include <mutex>
#include <optional>

namespace pineforge::hpo::detail {

struct NoImprovementSnapshot {
    std::uint64_t patience_trials = 0;
    std::optional<std::uint64_t> trigger_trial_id;
    std::optional<std::uint64_t> drained_through_trial_id;
};

// Only ordered coordinator feedback calls observe(). The watchdog may take a
// snapshot. This mutex is never held across sampler, RunState, or output calls.
class NoImprovementStop final {
public:
    NoImprovementStop(std::uint64_t patience, bool minimize)
        : minimize_(minimize) {
        state_.patience_trials = patience;
    }

    bool observe(std::uint64_t trial_id, bool feasible, std::optional<double> objective) {
        const std::lock_guard<std::mutex> lock(mutex_);
        if (state_.patience_trials == 0)
            return false;
        state_.drained_through_trial_id = trial_id;
        if (state_.trigger_trial_id)
            return true;
        if (feasible && objective && std::isfinite(*objective) &&
            (!best_ || (minimize_ ? *objective < *best_ : *objective > *best_))) {
            best_ = objective;
            non_improving_ = 0;
            return false;
        }
        if (!best_)
            return false;
        // Saturating at patience also handles UINT64_MAX without wrapping.
        if (non_improving_ < state_.patience_trials)
            ++non_improving_;
        if (non_improving_ == state_.patience_trials) {
            state_.trigger_trial_id = trial_id;
            return true;
        }
        return false;
    }

    NoImprovementSnapshot snapshot() const {
        const std::lock_guard<std::mutex> lock(mutex_);
        return state_;
    }

private:
    const bool minimize_;
    std::optional<double> best_;
    std::uint64_t non_improving_ = 0;
    NoImprovementSnapshot state_;
    mutable std::mutex mutex_;
};

}  // namespace pineforge::hpo::detail
