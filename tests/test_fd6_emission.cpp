#include "fd6_emission.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

namespace {

using pineforge::hpo::HpoError;
using pineforge::hpo::TypedHpoError;
using pineforge::hpo::cli::fd6::Fd6EmissionProjector;
using pineforge::hpo::cli::fd6::ProducerTrial;
using pineforge::hpo::cli::fd6::ProjectedTrial;

template <typename T, typename = void>
struct has_reason : std::false_type {};

template <typename T>
struct has_reason<T, std::void_t<decltype(std::declval<T>().reason)>> : std::true_type {};

template <typename T, typename = void>
struct has_native_status : std::false_type {};

template <typename T>
struct has_native_status<T, std::void_t<decltype(std::declval<T>().native_status)>>
    : std::true_type {};

template <typename T, typename = void>
struct has_failure_reason : std::false_type {};

template <typename T>
struct has_failure_reason<T, std::void_t<decltype(std::declval<T>().failure_reason)>>
    : std::true_type {};

static_assert(!has_reason<ProjectedTrial>::value);
static_assert(!has_native_status<ProjectedTrial>::value);
static_assert(!has_failure_reason<ProjectedTrial>::value);

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

void require_equal(const std::string& actual,
                   const std::string& expected,
                   const std::string& message) {
    require(actual == expected, message + ": got `" + actual + "`, expected `" + expected + "`");
}

void require_trial_id_argument(const HpoError& error,
                               std::uint64_t expected,
                               const std::string& message) {
    const auto argument = error.args().find("trial_id");
    require(argument != error.args().end(), message + ": missing trial_id argument");
    const auto* value = std::get_if<std::uint64_t>(&argument->second.value());
    require(value && *value == expected, message + ": wrong numeric trial_id argument");
}

template <typename Operation>
void require_id_refusal(const char* expected_code,
                        std::uint64_t expected_trial_id,
                        Operation&& operation) {
    try {
        operation();
    } catch (const TypedHpoError<std::invalid_argument>& error) {
        require_equal(error.code(), expected_code, "wrong FD6 refusal code");
        require(error.args().size() == 1, "FD6 ID refusal carried unexpected arguments");
        require_trial_id_argument(error, expected_trial_id, "FD6 ID refusal");
        return;
    }
    throw std::runtime_error("FD6 refusal was accepted");
}

void require_refusal(Fd6EmissionProjector& projector,
                     const char* expected_code,
                     const ProducerTrial& record) {
    require_id_refusal(expected_code, record.trial_id, [&] { projector.observe(record); });
}

void require_journal_entry(const Fd6EmissionProjector& projector,
                           std::size_t index,
                           const ProducerTrial& expected,
                           bool terminal) {
    const auto& journal = projector.journal();
    require(index < journal.size(), "missing FD6 journal entry");
    const auto& entry = journal[index];
    require(entry.trial_id == expected.trial_id, "journal retained the wrong trial ID");
    require_equal(entry.native_status, expected.status, "journal changed the native status");
    require(entry.feasible == expected.feasible, "journal changed producer feasibility");
    require(entry.terminal == terminal, "journal changed the terminal marker");
}

template <typename Projector>
bool journal_has(const Projector& projector, std::uint64_t trial_id) {
    for (const auto& entry : projector.journal()) {
        if (entry.trial_id == trial_id)
            return true;
    }
    return false;
}

void test_status_mapping_and_internal_status_retention() {
    struct MappingCase {
        const char* native_status;
        const char* projected_status;
        bool input_feasible;
        bool projected_feasible;
    };
    const std::vector<MappingCase> cases{
        {"ok", "ok", true, true},
        {"constraint_violation", "ok", true, false},
        {"pruned", "pruned", false, false},
        {"pruned", "pruned", true, true},
        {"engine_error", "failed", false, false},
        {"objective_error", "failed", false, false},
        {"constraint_error", "failed", false, false},
        {"trial_error", "failed", false, false},
        {"trial_timeout", "failed", false, false},
    };

    Fd6EmissionProjector projector(100);
    for (std::size_t index = 0; index < cases.size(); ++index) {
        const auto trial_id = static_cast<std::uint64_t>(100 + index);
        projector.issue(trial_id);
        projector.observe({trial_id, cases[index].native_status, cases[index].input_feasible});
    }

    const auto flush = projector.flush();
    require(flush.rows.size() == cases.size(), "mapping flush lost a producer record");
    require(flush.watermark && *flush.watermark == 100 + cases.size() - 1,
            "mapping flush did not advance the contiguous watermark");
    require(!flush.terminal, "ordinary mapping flush was marked terminal");
    require(projector.refusal_count() == 0, "mapping inputs caused a refusal");

    const auto& journal = projector.journal();
    require(journal.size() == cases.size(), "journal lost native producer evidence");
    for (std::size_t index = 0; index < cases.size(); ++index) {
        const auto& row = flush.rows[index];
        const auto& expected = cases[index];
        require(row.trial_id == 100 + index, "mapping flush is not ordered by trial ID");
        require_equal(row.status, expected.projected_status, "wrong projected status");
        require(row.feasible == expected.projected_feasible,
                "wrong projected feasibility");
        require(!row.beyond_watermark, "contiguous mapping row marked beyond watermark");
        require(journal[index].trial_id == row.trial_id, "journal ID differs from projection");
        require_equal(journal[index].native_status, expected.native_status,
                      "native status was not retained in the journal");
        require(journal[index].feasible == expected.input_feasible && journal[index].terminal,
                "mapping journal changed producer feasibility or terminal evidence");
    }
}

void test_unknown_status_is_typed_and_not_emitted() {
    Fd6EmissionProjector projector(200);
    projector.issue(200);
    bool refused = false;
    try {
        projector.observe({200, "future_status", true});
    } catch (const TypedHpoError<std::invalid_argument>& error) {
        refused = true;
        require_equal(error.code(), "hpo_fd6_unknown_status", "wrong FD6 refusal code");
        require_trial_id_argument(error, 200, "unknown-status refusal");
        require(error.args().size() == 1, "unknown-status refusal carried unexpected arguments");
        require(error.args().find("status") == error.args().end(),
                "unknown status leaked an unbounded status argument");
    }
    require(refused, "unknown status was accepted without a typed refusal");
    require(projector.refusal_count() == 1, "unknown status refusal was not counted");
    require(projector.journal().empty(), "unknown status entered the internal journal");
    require(projector.flush().rows.empty(), "unknown status produced a projected row");
}

void test_duplicate_issue_preserves_the_issued_trial() {
    const ProducerTrial terminal_input{700, "ok", true};
    Fd6EmissionProjector projector(terminal_input.trial_id);
    projector.issue(terminal_input.trial_id);
    require_id_refusal("hpo_fd6_duplicate_issue", terminal_input.trial_id,
                       [&] { projector.issue(terminal_input.trial_id); });
    require(projector.refusal_count() == 1 && projector.rejected_slots() == 0 &&
                projector.gap_count() == 0 && projector.journal().empty(),
            "duplicate issue changed evidence or gap counters");

    const auto before_terminal = projector.flush();
    require(!before_terminal.terminal && !before_terminal.watermark &&
                before_terminal.rows.empty(),
            "duplicate issue fabricated a terminal row or watermark");

    projector.observe(terminal_input);
    const auto flush = projector.flush();
    require(!flush.terminal && flush.watermark &&
                *flush.watermark == terminal_input.trial_id && flush.rows.size() == 1,
            "duplicate issue lost the original issued trial");
    const auto& row = flush.rows.front();
    require(row.trial_id == terminal_input.trial_id && row.status == "ok" &&
                row.feasible == terminal_input.feasible && !row.beyond_watermark,
            "duplicate issue changed the later terminal projection");
    require(projector.journal().size() == 1, "duplicate issue changed journal cardinality");
    require_journal_entry(projector, 0, terminal_input, true);
    require(projector.refusal_count() == 1 && projector.rejected_slots() == 0 &&
                projector.gap_count() == 0,
            "terminal acceptance after duplicate issue changed counters");
}

void test_unissued_and_duplicate_terminal_are_counted_and_not_emitted() {
    Fd6EmissionProjector projector(300);
    projector.issue(300);
    projector.observe({300, "ok", true});
    require_refusal(projector, "hpo_fd6_unissued_trial", {301, "engine_error", false});
    require_refusal(projector, "hpo_fd6_duplicate_terminal", {300, "trial_error", false});
    require(projector.refusal_count() == 2, "ID refusals were not counted");

    const auto flush = projector.flush();
    require(flush.rows.size() == 1, "refused IDs entered the emission");
    require(flush.rows.front().trial_id == 300, "wrong row survived ID refusals");
    require(projector.journal().size() == 1, "refused IDs entered the journal");
    require_equal(projector.journal().front().native_status, "ok",
                  "duplicate terminal replaced original journal evidence");
}

void test_pending_hole_holds_watermark_until_terminal_arrives() {
    Fd6EmissionProjector projector(400);
    projector.issue(400);
    projector.issue(401);
    projector.issue(402);
    projector.observe({400, "pending", false});
    projector.observe({402, "ok", true});

    auto beyond = projector.flush();
    require(!beyond.watermark, "pending hole incorrectly advanced the watermark");
    require(beyond.rows.size() == 1 && beyond.rows.front().trial_id == 402,
            "terminal after pending hole was not emitted");
    require(beyond.rows.front().beyond_watermark,
            "terminal after pending hole was not marked beyond watermark");
    require(projector.journal().size() == 2, "pending/terminal input evidence was lost");

    projector.observe({400, "engine_error", false});
    auto first = projector.flush();
    require(first.watermark && *first.watermark == 400,
            "terminal replacement did not close the pending hole");
    require(first.rows.size() == 1 && first.rows.front().trial_id == 400,
            "pending-hole terminal was not emitted");
    require(!first.rows.front().beyond_watermark,
            "terminal that closes the prefix was marked beyond watermark");

    projector.observe({401, "constraint_violation", true});
    auto complete = projector.flush();
    require(complete.watermark && *complete.watermark == 402,
            "watermark did not cross the completed terminal prefix");
    require(complete.rows.size() == 1 && complete.rows.front().trial_id == 401,
            "row filling the middle of the prefix was not emitted");
    require(!complete.rows.front().beyond_watermark,
            "row filling the prefix was marked beyond watermark");
    require(projector.refusal_count() == 0, "pending-hole inputs caused a refusal");
}

void test_skipped_hole_holds_watermark() {
    Fd6EmissionProjector projector(500);
    projector.issue(500);
    projector.issue(501);
    projector.skip(500);
    const ProducerTrial terminal_input{501, "ok", true};
    projector.observe(terminal_input);

    const auto flush = projector.flush();
    require(!flush.watermark, "skipped hole incorrectly advanced the watermark");
    require(flush.rows.size() == 1 && flush.rows.front().trial_id == 501,
            "row after skipped hole was not emitted");
    require(flush.rows.front().beyond_watermark,
            "row after skipped hole was not marked beyond watermark");
    const auto& journal = projector.journal();
    require(journal.size() == 1, "accepted terminal input was not journaled exactly once");
    require(journal.front().trial_id == terminal_input.trial_id,
            "journal retained the wrong terminal trial ID");
    require_equal(journal.front().native_status, terminal_input.status,
                  "journal did not retain the original native status");
    require(journal.front().feasible == terminal_input.feasible,
            "journal did not retain the producer feasibility");
    require(journal.front().terminal, "accepted terminal input was not marked terminal");
    require(journal.front().trial_id != 500, "skipped trial entered terminal journal evidence");
    require(projector.refusal_count() == 0, "skipped-hole inputs caused a refusal");
}

void test_out_of_order_arrival_is_sorted_within_one_flush() {
    const std::vector<ProducerTrial> arrivals{
        {603, "pruned", false},
        {601, "engine_error", false},
        {600, "ok", true},
        {602, "constraint_violation", true},
    };
    auto expected = arrivals;
    std::sort(expected.begin(), expected.end(),
              [](const auto& left, const auto& right) { return left.trial_id < right.trial_id; });

    Fd6EmissionProjector projector(600);
    for (const auto& trial : arrivals)
        projector.issue(trial.trial_id);
    for (const auto& trial : arrivals)
        projector.observe(trial);

    const auto flush = projector.flush();
    require(flush.rows.size() == expected.size(), "out-of-order flush lost a terminal row");
    require(flush.watermark && *flush.watermark == expected.back().trial_id,
            "out-of-order flush did not compute the terminal watermark");
    for (std::size_t index = 0; index < expected.size(); ++index) {
        require(flush.rows[index].trial_id == expected[index].trial_id,
                "out-of-order arrivals were emitted in arrival order");
        require(!flush.rows[index].beyond_watermark,
                "complete out-of-order prefix was marked beyond watermark");
    }
}

void test_skip_and_pending_refusals_preserve_state() {
    const ProducerTrial terminal_input{710, "constraint_violation", true};
    const std::uint64_t skipped_id = 711;
    Fd6EmissionProjector projector(terminal_input.trial_id);
    projector.issue(terminal_input.trial_id);
    projector.issue(skipped_id);
    projector.observe(terminal_input);
    projector.skip(skipped_id);

    const auto require_unchanged = [&](std::uint64_t refusals) {
        require(projector.refusal_count() == refusals && projector.rejected_slots() == 0 &&
                    projector.gap_count() == 0,
                "skip or pending refusal changed the wrong counters");
        require(projector.journal().size() == 1,
                "skip or pending refusal changed journal cardinality");
        require_journal_entry(projector, 0, terminal_input, true);
    };

    require_refusal(projector, "hpo_fd6_pending_after_terminal",
                    {terminal_input.trial_id, "pending", false});
    require_unchanged(1);
    require_id_refusal("hpo_fd6_skip_after_terminal", terminal_input.trial_id,
                       [&] { projector.skip(terminal_input.trial_id); });
    require_unchanged(2);
    require_refusal(projector, "hpo_fd6_pending_after_skip", {skipped_id, "pending", true});
    require_unchanged(3);
    require_id_refusal("hpo_fd6_duplicate_skip", skipped_id, [&] { projector.skip(skipped_id); });
    require_unchanged(4);

    const auto flush = projector.flush();
    require(!flush.terminal && flush.watermark &&
                *flush.watermark == terminal_input.trial_id && flush.rows.size() == 1,
            "skip or pending refusal changed the terminal prefix");
    const auto& row = flush.rows.front();
    require(row.trial_id == terminal_input.trial_id && row.status == "ok" &&
                !row.feasible && !row.beyond_watermark,
            "skip or pending refusal changed the accepted terminal row");
    require_unchanged(4);

    const auto final = projector.flush(true);
    require(final.terminal && final.rows.empty() && final.watermark == flush.watermark &&
                projector.gap_count() == 1 && projector.rejected_slots() == 0 &&
                projector.refusal_count() == 4,
            "skip or pending refusal changed the final skipped-hole state");
    require(projector.journal().size() == 1 && !journal_has(projector, skipped_id),
            "skip or pending refusal fabricated skipped-trial evidence");
    require_journal_entry(projector, 0, terminal_input, true);
}

void test_floor_rejection_is_typed_counted_and_not_issued() {
    Fd6EmissionProjector projector(200);
    projector.issue(200);
    projector.observe({200, "ok", true});
    const auto before_refusal = projector.refusal_count();
    const auto before_rejected = projector.rejected_slots();
    const auto before_gaps = projector.gap_count();

    bool below_floor_refused = false;
    try {
        projector.issue(199);
    } catch (const HpoError& error) {
        below_floor_refused = true;
        require_equal(error.code(), "hpo_fd6_below_floor",
                      "wrong below-floor refusal code");
        require(error.args().find("status") == error.args().end(),
                "below-floor refusal carried an unbounded status argument");
        require_trial_id_argument(error, 199, "below-floor refusal");
    }
    require(below_floor_refused, "below-floor ID was accepted");
    require(projector.refusal_count() == before_refusal + 1,
            "below-floor refusal did not increment refusal_count");
    require(projector.rejected_slots() == before_rejected + 1,
            "below-floor refusal did not increment rejected_slots");
    require(projector.gap_count() == before_gaps + 1,
            "below-floor refusal did not increment gap_count");

    require_refusal(projector, "hpo_fd6_unissued_trial", {199, "ok", true});
    require(projector.refusal_count() == before_refusal + 2,
            "unissued observation was not counted after below-floor refusal");
    require(projector.rejected_slots() == before_rejected + 1,
            "unissued observation changed rejected_slots");
    require(projector.gap_count() == before_gaps + 1,
            "unissued observation changed gap_count");

    const auto flush = projector.flush();
    require(!flush.terminal, "ordinary floor-boundary flush was marked terminal");
    require(flush.rows.size() == 1 && flush.rows.front().trial_id == 200,
            "floor-boundary flush lost the valid terminal row");
    require(projector.journal().size() == 1 && !journal_has(projector, 199),
            "below-floor or unissued ID entered the journal");
}

void test_skip_then_terminal_before_final_is_allowed() {
    Fd6EmissionProjector projector(800);
    projector.issue(800);
    projector.issue(801);
    projector.skip(800);
    projector.observe({801, "ok", true});

    const auto beyond = projector.flush();
    require(!beyond.watermark, "skip hole advanced the watermark");
    require(!beyond.terminal, "ordinary skip flush was marked terminal");
    require(beyond.rows.size() == 1 && beyond.rows.front().trial_id == 801 &&
                beyond.rows.front().beyond_watermark,
            "terminal after skip was not emitted beyond the watermark");

    projector.observe({800, "engine_error", false});
    const auto closed = projector.flush();
    require(closed.watermark && *closed.watermark == 801,
            "terminal after skip did not close the skipped hole");
    require(closed.rows.size() == 1 && closed.rows.front().trial_id == 800 &&
                closed.rows.front().status == "failed" &&
                !closed.rows.front().feasible && !closed.rows.front().beyond_watermark,
            "terminal after skip projected the wrong row");
    require(projector.journal().size() == 2 && journal_has(projector, 800) &&
                journal_has(projector, 801),
            "terminal after skip did not retain both producer records");

    require_refusal(projector, "hpo_fd6_duplicate_terminal",
                    {800, "trial_error", false});
    require(projector.refusal_count() == 1,
            "duplicate terminal after skip was not counted");

    const auto final = projector.flush(true);
    require(final.terminal && final.rows.empty() && final.watermark &&
                *final.watermark == 801,
            "final flush changed a fully terminal skipped sequence");
    require(projector.gap_count() == 0,
            "terminal after skip was incorrectly counted as a final gap");
}

void test_final_flush_closes_holes_once_without_fabricated_rows() {
    const ProducerTrial pending_input{900, "pending", false};
    const ProducerTrial terminal_input{902, "ok", true};
    Fd6EmissionProjector projector(900);
    projector.issue(900);
    projector.issue(901);
    projector.issue(902);
    projector.observe(pending_input);
    projector.skip(901);
    projector.observe(terminal_input);

    const auto nonfinal = projector.flush();
    require(!nonfinal.terminal && !nonfinal.watermark,
            "nonfinal hole flush crossed or sealed an open sequence");
    require(nonfinal.rows.size() == 1 && nonfinal.rows.front().trial_id == 902 &&
                nonfinal.rows.front().beyond_watermark,
            "nonfinal hole flush did not preserve the legitimate later row");
    require(projector.journal().size() == 2 && journal_has(projector, 900) &&
                journal_has(projector, 902) && !journal_has(projector, 901),
            "nonfinal hole flush changed journal evidence");

    const auto before_journal_size = projector.journal().size();
    const auto before_refusal = projector.refusal_count();
    const auto before_rejected = projector.rejected_slots();
    const auto before_gaps = projector.gap_count();
    const auto final = projector.flush(true);
    require(final.terminal && final.rows.empty() && !final.watermark,
            "final hole flush emitted a fabricated row or watermark");
    require(projector.journal().size() == before_journal_size &&
                !journal_has(projector, 901),
            "final hole flush fabricated journal evidence");
    require(projector.rejected_slots() == before_rejected,
            "final holes changed rejected_slots");
    require(projector.gap_count() == before_gaps + 2,
            "final holes were not counted exactly once");

    const auto repeated = projector.flush(true);
    require(repeated.terminal && repeated.rows.empty() && !repeated.watermark,
            "repeated final flush emitted or changed terminal state");
    require(projector.journal().size() == before_journal_size &&
                projector.rejected_slots() == before_rejected &&
                projector.gap_count() == before_gaps + 2 &&
                projector.refusal_count() == before_refusal,
            "repeated final flush changed evidence or counters");

    const auto require_sealed_state = [&](std::uint64_t refusals) {
        require(projector.journal().size() == before_journal_size &&
                    projector.rejected_slots() == before_rejected &&
                    projector.gap_count() == before_gaps + 2 &&
                    projector.refusal_count() == refusals,
                "post-final operation changed sealed evidence or counters");
        require_journal_entry(projector, 0, pending_input, false);
        require_journal_entry(projector, 1, terminal_input, true);
    };

    require_refusal(projector, "hpo_fd6_finalized", terminal_input);
    require_sealed_state(before_refusal + 1);
    require_id_refusal("hpo_fd6_finalized", 903, [&] { projector.issue(903); });
    require_sealed_state(before_refusal + 2);
    require_id_refusal("hpo_fd6_finalized", pending_input.trial_id,
                       [&] { projector.skip(pending_input.trial_id); });
    require_sealed_state(before_refusal + 3);

    bool nonfinal_refused = false;
    try {
        projector.flush();
    } catch (const TypedHpoError<std::logic_error>& error) {
        nonfinal_refused = true;
        require_equal(error.code(), "hpo_fd6_finalized", "wrong sealed-flush refusal code");
        require(error.args().empty(), "sealed-flush refusal carried unexpected arguments");
    }
    require(nonfinal_refused, "nonfinal flush after sealing was accepted");
    require_sealed_state(before_refusal + 4);

    const auto still_sealed = projector.flush(true);
    require(still_sealed.terminal && still_sealed.rows.empty() &&
                still_sealed.watermark == final.watermark,
            "refused post-final operations reopened the projector");
    require_sealed_state(before_refusal + 4);
}

void test_final_flush_emits_unflushed_rows_in_order_with_a_hole() {
    const ProducerTrial pending_input{1001, "pending", false};
    const std::vector<ProducerTrial> arrivals{
        {1003, "pruned", true},
        {1000, "constraint_violation", true},
        {1002, "engine_error", true},
    };
    const std::vector<ProjectedTrial> expected{
        {1000, "ok", false, false},
        {1002, "failed", false, true},
        {1003, "pruned", true, true},
    };
    Fd6EmissionProjector projector(1000);
    for (std::uint64_t trial_id = 1000; trial_id < 1004; ++trial_id)
        projector.issue(trial_id);
    projector.observe(pending_input);
    for (const auto& trial : arrivals)
        projector.observe(trial);

    const auto require_producer_evidence = [&] {
        require(projector.journal().size() == arrivals.size() + 1,
                "final flush changed producer journal cardinality");
        require_journal_entry(projector, 0, pending_input, false);
        for (std::size_t index = 0; index < arrivals.size(); ++index)
            require_journal_entry(projector, index + 1, arrivals[index], true);
    };

    const auto final = projector.flush(true);
    require(final.terminal && final.watermark && *final.watermark == 1000,
            "final flush crossed the pending hole or lost its terminal flag");
    require(final.rows.size() == expected.size(),
            "final flush lost a legitimate row or fabricated a hole row");
    for (std::size_t index = 0; index < expected.size(); ++index) {
        const auto& row = final.rows[index];
        const auto& wanted = expected[index];
        require(row.trial_id == wanted.trial_id,
                "final flush did not sort unflushed terminal rows");
        require_equal(row.status, wanted.status, "final flush changed projected status");
        require(row.feasible == wanted.feasible &&
                    row.beyond_watermark == wanted.beyond_watermark,
                "final flush changed feasibility or beyond-watermark flags");
    }
    require(projector.refusal_count() == 0 && projector.rejected_slots() == 0 &&
                projector.gap_count() == 1,
            "final flush did not count its single issued hole exactly once");
    require_producer_evidence();

    const auto repeated = projector.flush(true);
    require(repeated.terminal && repeated.rows.empty() && repeated.watermark == final.watermark,
            "repeated final flush re-emitted rows or changed the terminal watermark");
    require(projector.refusal_count() == 0 && projector.rejected_slots() == 0 &&
                projector.gap_count() == 1,
            "repeated final flush recounted the issued hole or changed counters");
    require_producer_evidence();
}

void test_uint64_max_boundary_does_not_wrap_watermark() {
    const auto maximum = std::numeric_limits<std::uint64_t>::max();
    Fd6EmissionProjector projector(maximum);
    projector.issue(maximum);
    projector.observe({maximum, "ok", true});

    const auto ordinary = projector.flush();
    require(!ordinary.terminal && ordinary.watermark &&
                *ordinary.watermark == maximum && ordinary.rows.size() == 1 &&
                ordinary.rows.front().trial_id == maximum &&
                !ordinary.rows.front().beyond_watermark,
            "UINT64_MAX terminal advanced or wrapped the watermark incorrectly");

    const auto final = projector.flush(true);
    require(final.terminal && final.watermark && *final.watermark == maximum &&
                final.rows.empty() && projector.gap_count() == 0,
            "UINT64_MAX final flush changed the terminal boundary");
    const auto repeated = projector.flush(true);
    require(repeated.terminal && repeated.watermark && *repeated.watermark == maximum &&
                repeated.rows.empty() && projector.gap_count() == 0,
            "UINT64_MAX repeated final flush was not idempotent");

    Fd6EmissionProjector below_maximum(maximum);
    bool refused = false;
    try {
        below_maximum.issue(maximum - 1);
    } catch (const HpoError& error) {
        refused = true;
        require_equal(error.code(), "hpo_fd6_below_floor",
                      "wrong UINT64_MAX floor refusal code");
        require_trial_id_argument(error, maximum - 1, "UINT64_MAX floor refusal");
    }
    require(refused && below_maximum.refusal_count() == 1 &&
                below_maximum.rejected_slots() == 1 && below_maximum.gap_count() == 1 &&
                below_maximum.journal().empty(),
            "UINT64_MAX below-floor refusal wrapped or entered state");
}

}  // namespace

int main() {
    try {
        test_status_mapping_and_internal_status_retention();
        test_unknown_status_is_typed_and_not_emitted();
        test_duplicate_issue_preserves_the_issued_trial();
        test_unissued_and_duplicate_terminal_are_counted_and_not_emitted();
        test_pending_hole_holds_watermark_until_terminal_arrives();
        test_skipped_hole_holds_watermark();
        test_out_of_order_arrival_is_sorted_within_one_flush();
        test_skip_and_pending_refusals_preserve_state();
        test_floor_rejection_is_typed_counted_and_not_issued();
        test_skip_then_terminal_before_final_is_allowed();
        test_final_flush_closes_holes_once_without_fabricated_rows();
        test_final_flush_emits_unflushed_rows_in_order_with_a_hole();
        test_uint64_max_boundary_does_not_wrap_watermark();
    } catch (const std::exception& error) {
        std::cerr << "FD6 emission test failed: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
