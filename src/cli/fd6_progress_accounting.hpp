#pragma once

#include <cstdint>
#include <limits>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

namespace pineforge::hpo::cli::fd6 {

struct SlotKey {
    std::string attempt_id;
    std::uint64_t trial_seq;
};

struct ProgressSnapshot {
    std::uint64_t emitted_count;
    std::uint64_t projected_count;
    std::uint64_t rejected_slots;
    std::uint64_t gap_count;
};

namespace detail {

inline void validate_snapshot(const ProgressSnapshot& snapshot) {
    if (snapshot.projected_count > snapshot.emitted_count) {
        throw std::logic_error("FD6 snapshot projected count exceeds emitted count");
    }
    if (snapshot.rejected_slots > snapshot.emitted_count) {
        throw std::logic_error("FD6 snapshot rejected count exceeds emitted count");
    }
    if (snapshot.gap_count != snapshot.emitted_count - snapshot.projected_count) {
        throw std::logic_error("FD6 snapshot gap count is not derived from its counters");
    }
}

}  // namespace detail

class PublicSlotAccounting final {
public:
    explicit PublicSlotAccounting(std::string attempt_id) {
        if (attempt_id.empty())
            throw std::invalid_argument("FD6 attempt_id must not be empty");
        attempt_id_ = std::move(attempt_id);
    }

    void reject(const SlotKey& key, std::uint64_t native_trial_id) {
        validate_key(key);
        const auto found = slots_.find(key.trial_seq);
        if (found != slots_.end()) {
            switch (found->second.state) {
            case SlotState::Rejected:
                remember_native_evidence(found->second, native_trial_id);
                return;
            case SlotState::Projected:
                throw std::logic_error("FD6 accepted slot cannot regress to rejected");
            case SlotState::Closed:
                throw std::logic_error("FD6 closed hole cannot regress to rejected");
            }
        }

        const auto next_emitted = checked_increment(emitted_count_);
        const auto next_rejected = checked_increment(rejected_slots_);
        const auto result =
            slots_.emplace(key.trial_seq, SlotRecord{SlotState::Rejected, native_trial_id});
        if (!result.second)
            throw std::logic_error("FD6 slot insertion changed during rejection");
        emitted_count_ = next_emitted;
        rejected_slots_ = next_rejected;
    }

    void project(const SlotKey& key, std::uint64_t native_trial_id) {
        validate_key(key);
        const auto found = slots_.find(key.trial_seq);
        if (found != slots_.end()) {
            switch (found->second.state) {
            case SlotState::Rejected: {
                if (projected_count_ == emitted_count_) {
                    throw std::logic_error("FD6 rejected slot has no available gap to correct");
                }
                const auto next_projected = checked_increment(projected_count_);
                const auto next_rejected = checked_decrement(rejected_slots_);
                found->second.state = SlotState::Projected;
                remember_native_evidence(found->second, native_trial_id);
                projected_count_ = next_projected;
                rejected_slots_ = next_rejected;
                return;
            }
            case SlotState::Projected:
                remember_native_evidence(found->second, native_trial_id);
                return;
            case SlotState::Closed:
                throw std::logic_error("FD6 closed hole cannot become projected");
            }
        }

        const auto next_emitted = checked_increment(emitted_count_);
        const auto next_projected = checked_increment(projected_count_);
        const auto result =
            slots_.emplace(key.trial_seq, SlotRecord{SlotState::Projected, native_trial_id});
        if (!result.second)
            throw std::logic_error("FD6 slot insertion changed during projection");
        emitted_count_ = next_emitted;
        projected_count_ = next_projected;
    }

    void close_hole(const SlotKey& key) {
        validate_key(key);
        const auto found = slots_.find(key.trial_seq);
        if (found != slots_.end()) {
            switch (found->second.state) {
            case SlotState::Rejected:
                return;
            case SlotState::Projected:
                throw std::logic_error("FD6 accepted slot cannot become a closed hole");
            case SlotState::Closed:
                return;
            }
        }

        const auto next_emitted = checked_increment(emitted_count_);
        const auto result =
            slots_.emplace(key.trial_seq, SlotRecord{SlotState::Closed, std::nullopt});
        if (!result.second)
            throw std::logic_error("FD6 slot insertion changed during hole closure");
        emitted_count_ = next_emitted;
    }

    ProgressSnapshot snapshot() const {
        return ProgressSnapshot{
            emitted_count_, projected_count_, rejected_slots_,
            checked_gap(emitted_count_, projected_count_)};
    }

private:
    enum class SlotState : std::uint8_t { Rejected, Projected, Closed };

    struct SlotRecord {
        SlotState state;
        std::optional<std::uint64_t> native_trial_id;
    };

    static void remember_native_evidence(SlotRecord& record, std::uint64_t native_trial_id) {
        if (!record.native_trial_id || *record.native_trial_id != native_trial_id) {
            record.native_trial_id = native_trial_id;
        }
    }

    static std::uint64_t checked_increment(std::uint64_t value) {
        if (value == std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("FD6 slot accounting counter overflow");
        return value + 1;
    }

    static std::uint64_t checked_decrement(std::uint64_t value) {
        if (value == 0)
            throw std::logic_error("FD6 slot accounting counter underflow");
        return value - 1;
    }

    static std::uint64_t checked_gap(std::uint64_t emitted, std::uint64_t projected) {
        if (projected > emitted)
            throw std::logic_error("FD6 projected count exceeds emitted count");
        return emitted - projected;
    }

    void validate_key(const SlotKey& key) const {
        if (key.attempt_id.empty())
            throw std::invalid_argument("FD6 SlotKey attempt_id must not be empty");
        if (key.trial_seq == 0)
            throw std::invalid_argument("FD6 SlotKey trial_seq must be positive");
        if (key.attempt_id != attempt_id_)
            throw std::invalid_argument("FD6 SlotKey belongs to a different attempt");
    }

    std::string attempt_id_;
    std::map<std::uint64_t, SlotRecord> slots_;
    std::uint64_t emitted_count_ = 0;
    std::uint64_t projected_count_ = 0;
    std::uint64_t rejected_slots_ = 0;
};

inline std::string serialize_progress_gaps(const ProgressSnapshot& snapshot,
                                           std::uint64_t conflicting_slots) {
    detail::validate_snapshot(snapshot);
    return std::string("{\"rejectedSlots\":") + std::to_string(snapshot.rejected_slots) +
           ",\"conflictingSlots\":" + std::to_string(conflicting_slots) + "}";
}

inline std::string serialize_progress_accounting(const ProgressSnapshot& snapshot,
                                                 std::uint64_t write_failure_count) {
    detail::validate_snapshot(snapshot);
    return std::string("{\"gapCount\":") + std::to_string(snapshot.gap_count) +
           ",\"writeFailureCount\":" + std::to_string(write_failure_count) + "}";
}

}  // namespace pineforge::hpo::cli::fd6
