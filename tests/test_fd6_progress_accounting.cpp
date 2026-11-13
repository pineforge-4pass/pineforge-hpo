#include "fd6_progress_accounting.hpp"

#include "../src/core/json.hpp"

#include <cstdint>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>

namespace {

namespace detail = pineforge::hpo::detail;
namespace fd6 = pineforge::hpo::cli::fd6;
using Json = detail::Json;

void require(bool condition, std::string message) {
    if (!condition)
        throw std::runtime_error(std::move(message));
}

const Json& required_field(const Json& object, const char* name) {
    require(object.kind == Json::Kind::Object, "serialized value is not a JSON object");
    const auto found = object.members.find(name);
    require(found != object.members.end(),
            std::string("serialized value is missing required field ") + name);
    return found->second;
}

void require_exact_keys(const Json& object,
                        std::initializer_list<const char*> keys,
                        const std::string& label) {
    require(object.kind == Json::Kind::Object, label + " is not a JSON object");
    require(object.members.size() == keys.size(), label + " has unexpected JSON fields");
    for (const char* key : keys)
        require(object.members.find(key) != object.members.end(),
                label + " is missing JSON field " + key);
}

std::uint64_t json_uint(const Json& object, const char* name) {
    const Json& value = required_field(object, name);
    require(value.kind == Json::Kind::Number,
            std::string("JSON field is not an integer number: ") + name);
    require(!value.value.empty() && value.value.front() != '-',
            std::string("JSON field is negative: ") + name);
    std::size_t consumed = 0;
    unsigned long long parsed = 0;
    try {
        parsed = std::stoull(value.value, &consumed, 10);
    } catch (const std::exception& error) {
        throw std::runtime_error(std::string("JSON field is not an unsigned integer: ") + name +
                                 ": " + error.what());
    }
    require(consumed == value.value.size(),
            std::string("JSON field is not an unsigned integer: ") + name);
    require(parsed <= static_cast<unsigned long long>(
                           std::numeric_limits<std::uint64_t>::max()),
            std::string("JSON field is outside uint64_t range: ") + name);
    return static_cast<std::uint64_t>(parsed);
}

Json parse_object(const std::string& serialized, const std::string& label) {
    Json object = detail::parse_json(serialized);
    require(object.kind == Json::Kind::Object, label + " did not serialize an object");
    return object;
}

template <typename Snapshot>
void require_counts(const Snapshot& snapshot,
                    std::uint64_t emitted,
                    std::uint64_t projected,
                    std::uint64_t rejected,
                    std::uint64_t gaps,
                    const std::string& label) {
    require(snapshot.emitted_count >= snapshot.projected_count,
            label + " has projected_count above emitted_count");
    require(snapshot.gap_count == snapshot.emitted_count - snapshot.projected_count,
            label + " has a non-derived gap_count");
    require(snapshot.emitted_count == emitted, label + " emitted_count mismatch");
    require(snapshot.projected_count == projected, label + " projected_count mismatch");
    require(snapshot.rejected_slots == rejected, label + " rejected_slots mismatch");
    require(snapshot.gap_count == gaps, label + " gap_count mismatch");
}

template <typename Snapshot>
void record_row(const Snapshot& snapshot,
                const fd6::SlotKey& key,
                std::uint64_t native_trial_id,
                const std::string& scope_attempt_id,
                const std::string& label,
                std::uint64_t emitted,
                std::uint64_t projected,
                std::uint64_t rejected,
                std::uint64_t gaps,
                std::uint64_t conflicting_slots = 0,
                std::uint64_t write_failure_count = 0) {
    require_counts(snapshot, emitted, projected, rejected, gaps, label);

    const std::string serialized_gaps =
        fd6::serialize_progress_gaps(snapshot, conflicting_slots);
    const Json gaps_object = parse_object(serialized_gaps, label + " ProgressGaps");
    require_exact_keys(gaps_object, {"rejectedSlots", "conflictingSlots"},
                       label + " ProgressGaps");
    require(json_uint(gaps_object, "rejectedSlots") == snapshot.rejected_slots,
            label + " rejectedSlots mismatch");
    require(json_uint(gaps_object, "conflictingSlots") == conflicting_slots,
            label + " conflictingSlots ignored or mismatched");

    const std::string serialized_accounting =
        fd6::serialize_progress_accounting(snapshot, write_failure_count);
    const Json accounting_object =
        parse_object(serialized_accounting, label + " ProgressAccounting");
    require_exact_keys(accounting_object, {"gapCount", "writeFailureCount"},
                       label + " ProgressAccounting");
    require(json_uint(accounting_object, "gapCount") == snapshot.gap_count,
            label + " gapCount mismatch");
    require(json_uint(accounting_object, "writeFailureCount") == write_failure_count,
            label + " writeFailureCount ignored or mismatched");

    std::cout << "row=" << label << " scope_attempt_id=" << scope_attempt_id
              << " key_attempt_id=" << key.attempt_id << " trial_seq=" << key.trial_seq
              << " native_trial_id=" << native_trial_id
              << " emitted_count=" << snapshot.emitted_count
              << " projected_count=" << snapshot.projected_count
              << " rejected_slots=" << snapshot.rejected_slots
              << " gap_count=" << snapshot.gap_count
              << " progress_gaps=" << serialized_gaps
              << " progress_accounting=" << serialized_accounting << '\n';
}

void run_replay() {
    const std::string attempt = "attempt-replay";
    fd6::PublicSlotAccounting accounting(attempt);
    const fd6::SlotKey slot{attempt, 1};

    accounting.reject(slot, 11);
    record_row(accounting.snapshot(), slot, 11, attempt, "replay/reject-first", 1, 0, 1, 1);

    accounting.reject(slot, 12);
    record_row(accounting.snapshot(), slot, 12, attempt, "replay/reject-replay", 1, 0, 1, 1);

    const fd6::SlotKey foreign_slot{"attempt-replay-other", 1};
    bool rejected = false;
    try {
        accounting.reject(foreign_slot, 13);
    } catch (const std::exception&) {
        rejected = true;
    }
    require(rejected, "replay accepted a SlotKey from a different attempt");
    record_row(accounting.snapshot(), foreign_slot, 13, attempt,
               "replay/foreign-attempt-rejected", 1, 0, 1, 1);
}

void run_same_id() {
    const std::string attempt = "attempt-same-id";
    fd6::PublicSlotAccounting accounting(attempt);
    const fd6::SlotKey first{attempt, 1};
    const fd6::SlotKey second{attempt, 2};
    constexpr std::uint64_t below_floor_native_id = 7;

    accounting.reject(first, below_floor_native_id);
    record_row(accounting.snapshot(), first, below_floor_native_id, attempt,
               "same-id/first-line", 1, 0, 1, 1);

    accounting.reject(second, below_floor_native_id);
    record_row(accounting.snapshot(), second, below_floor_native_id, attempt,
               "same-id/second-line-same-native-id", 2, 0, 2, 2);

    accounting.reject(first, below_floor_native_id);
    record_row(accounting.snapshot(), first, below_floor_native_id, attempt,
               "same-id/first-line-replay", 2, 0, 2, 2);
}

void run_correction() {
    const std::string attempt = "attempt-correction";
    fd6::PublicSlotAccounting accounting(attempt);
    const fd6::SlotKey corrected{attempt, 1};
    const fd6::SlotKey remaining{attempt, 2};

    accounting.reject(corrected, 101);
    record_row(accounting.snapshot(), corrected, 101, attempt,
               "correction/first-rejection", 1, 0, 1, 1);

    accounting.reject(remaining, 102);
    record_row(accounting.snapshot(), remaining, 102, attempt,
               "correction/second-rejection", 2, 0, 2, 2);

    accounting.project(corrected, 901);
    record_row(accounting.snapshot(), corrected, 901, attempt,
               "correction/legitimate-correction", 2, 1, 1, 1, 3, 5);

    accounting.project(corrected, 902);
    record_row(accounting.snapshot(), corrected, 902, attempt,
               "correction/accepted-replay", 2, 1, 1, 1, 8, 13);
}

void run_terminal() {
    const std::string attempt = "attempt-terminal";
    fd6::PublicSlotAccounting accounting(attempt);
    const fd6::SlotKey rejected_slot{attempt, 1};
    const fd6::SlotKey pending_hole{attempt, 2};
    const fd6::SlotKey projected_slot{attempt, 3};

    accounting.reject(rejected_slot, 301);
    record_row(accounting.snapshot(), rejected_slot, 301, attempt,
               "terminal/rejected-slot", 1, 0, 1, 1);

    accounting.close_hole(pending_hole);
    record_row(accounting.snapshot(), pending_hole, 0, attempt,
               "terminal/pending-hole-closed", 2, 0, 1, 2);

    accounting.project(projected_slot, 303);
    record_row(accounting.snapshot(), projected_slot, 303, attempt,
               "terminal/projected-slot", 3, 1, 1, 2);

    accounting.close_hole(pending_hole);
    record_row(accounting.snapshot(), pending_hole, 0, attempt,
               "terminal/pending-hole-close-replay", 3, 1, 1, 2);
}

void run_selector(const std::string& selector) {
    if (selector == "replay")
        return run_replay();
    if (selector == "same-id")
        return run_same_id();
    if (selector == "correction")
        return run_correction();
    if (selector == "terminal")
        return run_terminal();
    throw std::invalid_argument("unknown selector: " + selector);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: pineforge_hpo_test_fd6_progress_accounting "
                     "<replay|same-id|correction|terminal>\n";
        return 2;
    }

    try {
        run_selector(argv[1]);
        require(static_cast<bool>(std::cout), "failed to write FD6 test evidence");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ERROR selector=" << argv[1] << " message=" << error.what() << '\n';
        return 1;
    } catch (...) {
        std::cerr << "ERROR selector=" << argv[1]
                  << " message=non-standard exception\n";
        return 1;
    }
}
