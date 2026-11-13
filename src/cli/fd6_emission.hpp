#pragma once

#include <cstdint>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <pineforge/hpo/error.hpp>

namespace pineforge::hpo::cli::fd6 {

/// The producer-side record consumed by the FD6 projection policy.
struct ProducerTrial {
    std::uint64_t trial_id = 0;
    std::string status = "pending";
    bool feasible = false;
};

/// The public policy fields produced for one emitted trial.
///
/// Native status is deliberately absent. The parent integration supplies the remaining public
/// trial payload and keeps the native status in its producer/journal evidence.
struct ProjectedTrial {
    std::uint64_t trial_id = 0;
    std::string status;
    bool feasible = false;
    bool beyond_watermark = false;
};

struct EmissionFlush {
    std::vector<ProjectedTrial> rows;
    std::optional<std::uint64_t> watermark;
    bool terminal = false;
};

namespace detail {

/// Producer evidence retained for the parent journal; this is not a public projected row.
struct Fd6JournalEntry {
    std::uint64_t trial_id = 0;
    std::string native_status;
    bool feasible = false;
    bool terminal = false;
};

}  // namespace detail

/// Applies the FD6 status, terminal-ID, floor, and watermark policy.
///
/// Issuance and observation are separate because the producer can reserve an ID and later skip
/// it. The floor is immutable for the projector lifetime. A final flush seals the projector:
/// open issued IDs become counted gaps without becoming terminal rows or journal entries.
class Fd6EmissionProjector final {
    enum class InputKind : std::uint8_t { kPending, kTerminal };
    enum class State : std::uint8_t { kPending, kTerminal, kSkipped };

    struct WatermarkCursor {
        std::optional<std::uint64_t> watermark;
        std::optional<std::uint64_t> next;
    };

public:
    /// Constructs a projector with an immutable inclusive trial-ID floor.
    explicit Fd6EmissionProjector(std::uint64_t floor = 0) noexcept
        : floor_(floor), next_watermark_id_(floor) {}

    /// Records one issued trial ID.
    void issue(std::uint64_t trial_id) {
        if (finalized_)
            refuse("hpo_fd6_finalized", trial_id, "FD6 projector is finalized");
        if (trial_id < floor_)
            refuse("hpo_fd6_below_floor", trial_id, "FD6 trial ID is below the projector floor",
                   true);
        if (!issued_.insert(trial_id).second)
            refuse("hpo_fd6_duplicate_issue", trial_id,
                   "FD6 trial ID was issued more than once");
    }

    /// Records a producer observation. Pending observations are journaled but never emitted.
    void observe(const ProducerTrial& trial) {
        if (finalized_)
            refuse("hpo_fd6_finalized", trial.trial_id, "FD6 projector is finalized");
        const auto kind = classify(trial.status);
        if (!kind)
            refuse("hpo_fd6_unknown_status", trial.trial_id, "FD6 producer status is unknown");
        if (issued_.find(trial.trial_id) == issued_.end())
            refuse("hpo_fd6_unissued_trial", trial.trial_id,
                   "FD6 producer record names an unissued trial ID");

        const auto state = states_.find(trial.trial_id);
        if (*kind == InputKind::kPending) {
            if (state != states_.end() && state->second == State::kTerminal)
                refuse("hpo_fd6_pending_after_terminal", trial.trial_id,
                       "FD6 pending record arrived after a terminal record");
            if (state != states_.end() && state->second == State::kSkipped)
                refuse("hpo_fd6_pending_after_skip", trial.trial_id,
                       "FD6 pending record arrived after a skipped trial");
            journal_.push_back({trial.trial_id, trial.status, trial.feasible, false});
            try {
                states_.try_emplace(trial.trial_id, State::kPending);
            } catch (...) {
                journal_.pop_back();
                throw;
            }
            return;
        }

        if (state != states_.end() && state->second == State::kTerminal)
            refuse("hpo_fd6_duplicate_terminal", trial.trial_id,
                   "FD6 trial ID received a second terminal record");

        auto projected = project(trial);
        auto pending_result = pending_.emplace(trial.trial_id, std::move(projected));
        if (!pending_result.second)
            throw TypedHpoError<std::logic_error>(
                "hpo_fd6_internal", {}, "FD6 terminal row was already pending");
        const auto pending = pending_result.first;
        // The snapshot is restored only when the state entry already existed.
        const auto previous_state =
            state == states_.end() ? State::kPending : state->second;
        auto state_result = states_.end();
        bool state_inserted = false;
        bool terminal_inserted = false;
        try {
            const auto inserted_state =
                states_.try_emplace(trial.trial_id, State::kTerminal);
            state_result = inserted_state.first;
            state_inserted = inserted_state.second;
            if (!state_inserted)
                state_result->second = State::kTerminal;
            const auto inserted_terminal = terminal_ids_.insert(trial.trial_id);
            terminal_inserted = inserted_terminal.second;
            if (!terminal_inserted)
                throw TypedHpoError<std::logic_error>(
                    "hpo_fd6_internal", {}, "FD6 terminal ID was already recorded");
            journal_.push_back({trial.trial_id, trial.status, trial.feasible, true});
        } catch (...) {
            if (terminal_inserted)
                terminal_ids_.erase(trial.trial_id);
            if (state_result != states_.end()) {
                if (state_inserted)
                    states_.erase(state_result);
                else
                    state_result->second = previous_state;
            }
            pending_.erase(pending);
            throw;
        }
    }

    /// Records an issued trial which will not produce a terminal row.
    void skip(std::uint64_t trial_id) {
        if (finalized_)
            refuse("hpo_fd6_finalized", trial_id, "FD6 projector is finalized");
        if (issued_.find(trial_id) == issued_.end())
            refuse("hpo_fd6_unissued_trial", trial_id,
                   "FD6 skipped record names an unissued trial ID");
        const auto state = states_.find(trial_id);
        if (state != states_.end() && state->second == State::kTerminal)
            refuse("hpo_fd6_skip_after_terminal", trial_id,
                   "FD6 trial was skipped after a terminal record");
        if (state != states_.end() && state->second == State::kSkipped)
            refuse("hpo_fd6_duplicate_skip", trial_id,
                   "FD6 trial ID was skipped more than once");
        states_.try_emplace(trial_id, State::kSkipped);
        if (state != states_.end())
            state->second = State::kSkipped;
    }

    /// Emits terminal rows in trial-ID order; final flush seals and closes open issued holes.
    EmissionFlush flush(bool final = false) {
        if (finalized_) {
            if (!final)
                refuse_sealed_flush();
            EmissionFlush result;
            result.watermark = watermark_;
            result.terminal = true;
            return result;
        }

        const auto cursor = watermark_after_terminals();
        const auto holes = final ? open_holes() : 0;
        if (final && holes > std::numeric_limits<std::uint64_t>::max() - gap_count_)
            counter_overflow();

        EmissionFlush result;
        result.watermark = cursor.watermark;
        result.terminal = final;
        result.rows.reserve(pending_.size());
        for (const auto& [trial_id, row] : pending_) {
            auto projected = row;
            projected.beyond_watermark =
                !cursor.watermark || trial_id > *cursor.watermark;
            result.rows.push_back(std::move(projected));
        }

        watermark_ = cursor.watermark;
        next_watermark_id_ = cursor.next;
        pending_.clear();
        if (final) {
            gap_count_ += holes;
            finalized_ = true;
        }
        return result;
    }

    /// Returns the number of refused input records or ID operations.
    std::uint64_t refusal_count() const noexcept { return refusal_count_; }

    /// Returns the cumulative number of below-floor rejected slots.
    std::uint64_t rejected_slots() const noexcept { return rejected_slots_; }

    /// Returns the cumulative number of below-floor or final-gap slots.
    std::uint64_t gap_count() const noexcept { return gap_count_; }

    /// Returns internal producer/journal evidence, including original native statuses.
    const std::vector<detail::Fd6JournalEntry>& journal() const noexcept { return journal_; }

private:
    static std::optional<InputKind> classify(const std::string& status) noexcept {
        if (status == "pending")
            return InputKind::kPending;
        if (status == "ok" || status == "constraint_violation" || status == "pruned" ||
            status == "engine_error" || status == "objective_error" ||
            status == "constraint_error" || status == "trial_error" ||
            status == "trial_timeout")
            return InputKind::kTerminal;
        return std::nullopt;
    }

    static ProjectedTrial project(const ProducerTrial& trial) {
        if (trial.status == "ok")
            return {trial.trial_id, "ok", trial.feasible, false};
        if (trial.status == "constraint_violation")
            return {trial.trial_id, "ok", false, false};
        if (trial.status == "pruned")
            return {trial.trial_id, "pruned", trial.feasible, false};
        return {trial.trial_id, "failed", false, false};
    }

    WatermarkCursor watermark_after_terminals() const {
        WatermarkCursor result{watermark_, next_watermark_id_};
        while (result.next && terminal_ids_.find(*result.next) != terminal_ids_.end()) {
            result.watermark = *result.next;
            if (*result.next == std::numeric_limits<std::uint64_t>::max()) {
                result.next.reset();
                break;
            }
            ++*result.next;
        }
        return result;
    }

    std::uint64_t open_holes() const {
        std::uint64_t holes = 0;
        for (const auto trial_id : issued_) {
            const auto state = states_.find(trial_id);
            if (state != states_.end() && state->second == State::kTerminal)
                continue;
            if (holes == std::numeric_limits<std::uint64_t>::max())
                counter_overflow();
            ++holes;
        }
        return holes;
    }

    void count_refusal(bool rejected_slot) {
        const auto maximum = std::numeric_limits<std::uint64_t>::max();
        if (refusal_count_ == maximum ||
            (rejected_slot && (rejected_slots_ == maximum || gap_count_ == maximum)))
            counter_overflow();
        ++refusal_count_;
        if (rejected_slot) {
            ++rejected_slots_;
            ++gap_count_;
        }
    }

    [[noreturn]] static void counter_overflow() {
        throw TypedHpoError<std::overflow_error>(
            "hpo_fd6_counter_overflow", {}, "FD6 counter arithmetic would overflow");
    }

    [[noreturn]] void refuse(const char* code,
                             std::uint64_t trial_id,
                             const char* message,
                             bool rejected_slot = false) {
        count_refusal(rejected_slot);
        ErrorArguments args;
        args.emplace("trial_id", ErrorArgument(trial_id));
        throw TypedHpoError<std::invalid_argument>(code, std::move(args), message);
    }

    [[noreturn]] void refuse_sealed_flush() {
        count_refusal(false);
        throw TypedHpoError<std::logic_error>(
            "hpo_fd6_finalized", {}, "FD6 projector is finalized");
    }

    const std::uint64_t floor_;
    std::optional<std::uint64_t> next_watermark_id_;
    std::optional<std::uint64_t> watermark_;
    std::set<std::uint64_t> issued_;
    std::map<std::uint64_t, State> states_;
    std::set<std::uint64_t> terminal_ids_;
    std::map<std::uint64_t, ProjectedTrial> pending_;
    std::vector<detail::Fd6JournalEntry> journal_;
    std::uint64_t refusal_count_ = 0;
    std::uint64_t rejected_slots_ = 0;
    std::uint64_t gap_count_ = 0;
    bool finalized_ = false;
};

}  // namespace pineforge::hpo::cli::fd6
