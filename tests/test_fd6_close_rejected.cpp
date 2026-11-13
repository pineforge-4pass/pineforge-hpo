#include "../src/cli/fd6_progress_accounting.hpp"
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

struct ExpectedCounts {
    std::uint64_t emitted = 0;
    std::uint64_t projected = 0;
    std::uint64_t rejected = 0;

    void add_rejected_slot() {
        ++emitted;
        ++rejected;
    }

    std::uint64_t gaps() const {
        require(projected <= emitted, "expected model projected count exceeds emitted count");
        return emitted - projected;
    }
};

std::string counts_text(const fd6::ProgressSnapshot& snapshot) {
    return "emitted=" + std::to_string(snapshot.emitted_count) +
           " projected=" + std::to_string(snapshot.projected_count) +
           " rejected=" + std::to_string(snapshot.rejected_slots) +
           " gap=" + std::to_string(snapshot.gap_count);
}

struct Observation {
    fd6::ProgressSnapshot snapshot;
    std::string progress_gaps;
    std::string progress_accounting;
};

Observation observe(const fd6::PublicSlotAccounting& accounting,
                    const fd6::SlotKey& key,
                    const char* operation,
                    const std::string& native_evidence,
                    std::uint64_t conflicting_slots,
                    std::uint64_t write_failure_count) {
    const fd6::ProgressSnapshot snapshot = accounting.snapshot();
    Observation observation{
        snapshot,
        fd6::serialize_progress_gaps(snapshot, conflicting_slots),
        fd6::serialize_progress_accounting(snapshot, write_failure_count)};
    std::cout << "operation=" << operation << " attempt_id=" << key.attempt_id
              << " trial_seq=" << key.trial_seq
              << " native_evidence=" << native_evidence
              << " actual=" << counts_text(observation.snapshot)
              << " progress_gaps=" << observation.progress_gaps
              << " progress_accounting=" << observation.progress_accounting << std::endl;
    return observation;
}

void require_observation(const Observation& observation,
                         const ExpectedCounts& expected,
                         std::uint64_t conflicting_slots,
                         std::uint64_t write_failure_count,
                         const std::string& label,
                         const Observation* before = nullptr) {
    const auto mismatch = [&](const std::string& detail) {
        std::string message = label + ": " + detail;
        if (before)
            message += "; before={" + counts_text(before->snapshot) + "}";
        message += "; after={" + counts_text(observation.snapshot) + "}";
        throw std::runtime_error(std::move(message));
    };

    if (observation.snapshot.rejected_slots != expected.rejected) {
        mismatch("rejection-erasure: rejectedSlots changed outside a legitimate correction");
    }
    if (observation.snapshot.emitted_count != expected.emitted ||
        observation.snapshot.projected_count != expected.projected ||
        observation.snapshot.gap_count != expected.gaps()) {
        mismatch("snapshot does not match input-derived expected counters");
    }

    const Json gaps = parse_object(observation.progress_gaps, label + " ProgressGaps");
    require_exact_keys(gaps, {"rejectedSlots", "conflictingSlots"},
                       label + " ProgressGaps");
    if (json_uint(gaps, "rejectedSlots") != expected.rejected) {
        mismatch("rejection-erasure: ProgressGaps rejectedSlots disagrees with the preserved "
                 "snapshot");
    }
    require(json_uint(gaps, "conflictingSlots") == conflicting_slots,
            label + " ProgressGaps ignored conflictingSlots");

    const Json accounting =
        parse_object(observation.progress_accounting, label + " ProgressAccounting");
    require_exact_keys(accounting, {"gapCount", "writeFailureCount"},
                       label + " ProgressAccounting");
    require(json_uint(accounting, "gapCount") == expected.gaps(),
            label + " ProgressAccounting gapCount mismatch");
    require(json_uint(accounting, "writeFailureCount") == write_failure_count,
            label + " ProgressAccounting ignored writeFailureCount");
}

void require_stable_replay(const Observation& before,
                           const Observation& after,
                           const ExpectedCounts& expected) {
    require(after.snapshot.emitted_count == before.snapshot.emitted_count &&
                after.snapshot.projected_count == before.snapshot.projected_count &&
                after.snapshot.rejected_slots == before.snapshot.rejected_slots &&
                after.snapshot.gap_count == before.snapshot.gap_count,
            "repeated close_hole changed counters; before={" + counts_text(before.snapshot) +
                "} after={" + counts_text(after.snapshot) + "}");
    require(after.progress_gaps == before.progress_gaps &&
                after.progress_accounting == before.progress_accounting,
            "repeated close_hole changed serialized evidence");
    require(after.snapshot.projected_count == expected.projected,
            "repeated close_hole fabricated an accepted projected row");
}

}  // namespace

int main(int argc, char**) {
    if (argc != 1) {
        std::cerr << "usage: fd6-close-rejected-control\n";
        return 2;
    }

    try {
        const std::string attempt = "A";
        const fd6::SlotKey slot{attempt, 1};
        constexpr std::uint64_t native_trial_id = 7;
        ExpectedCounts expected;
        fd6::PublicSlotAccounting accounting(attempt);

        accounting.reject(slot, native_trial_id);
        expected.add_rejected_slot();
        const Observation after_reject =
            observe(accounting, slot, "after-reject", std::to_string(native_trial_id), 3, 5);
        require_observation(after_reject, expected, 3, 5, "after-reject");

        accounting.close_hole(slot);
        const Observation after_close =
            observe(accounting, slot, "after-close-existing-rejected", "not-supplied", 11, 17);
        require_observation(after_close, expected, 11, 17,
                            "after-close-existing-rejected", &after_reject);

        accounting.close_hole(slot);
        const Observation after_replay =
            observe(accounting, slot, "after-close-replay", "not-supplied", 11, 17);
        require_observation(after_replay, expected, 11, 17,
                            "after-close-replay", &after_close);
        require_stable_replay(after_close, after_replay, expected);

        require(static_cast<bool>(std::cout), "failed to write semantic control evidence");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ERROR " << error.what() << '\n';
        return 1;
    } catch (...) {
        std::cerr << "ERROR non-standard exception\n";
        return 1;
    }
}
