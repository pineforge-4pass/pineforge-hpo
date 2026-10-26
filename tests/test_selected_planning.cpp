#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/selected_planning.hpp>

#include "../src/core/sha256.hpp"

#include <unistd.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <initializer_list>
#include <iostream>
#include <iterator>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <variant>
#include <vector>

namespace {

namespace pfh = pineforge::hpo;
namespace fs = std::filesystem;
using Access = pfh::detail::SelectedPlanningTestAccess;
using Raw = pf_selected_plan_result_v1;

const char* scenario = "setup";
constexpr std::int64_t kBase = 1704067200000LL;  // 2024-01-01 00:00 UTC.
constexpr std::int64_t kMinute = 60000;

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

template <typename Function>
void row(const char* name, Function function) {
    scenario = name;
    std::cout << "ROW " << name << " BEGIN\n" << std::flush;
    function();
    std::cout << "ROW " << name << " PASS\n" << std::flush;
}

// Same named-path refusal and private run-directory construction as test_window_plan.cpp.
void refuse_protected_name(const fs::path& path) {
    std::vector<std::string> names;
    for (const auto& component : path) {
        const auto name = component.string();
        require(name != ".ssh" && name != ".aws" && name != ".codex" &&
                    !(name.size() >= 4 && name.compare(name.size() - 4, 4, ".env") == 0),
                "refused protected path by name: " + path.string());
        names.push_back(name);
    }
    for (std::size_t index = 0; index < names.size(); ++index) {
        if (names[index] != ".config")
            continue;
        require(index + 1 >= names.size() || names[index + 1] != "pineforge",
                "refused protected path by name: " + path.string());
        require(index + 2 >= names.size() || names[index + 1] != "zsh" ||
                    names[index + 2] != "keys",
                "refused protected path by name: " + path.string());
    }
}

// List a path before following it, including every expanded symlink target.
fs::path checked_path(const fs::path& path, const char* purpose) {
    refuse_protected_name(path);
    auto pending = fs::absolute(path);
    refuse_protected_name(pending);
    pending = pending.lexically_normal();
    refuse_protected_name(pending);
    std::cout << "FILES " << purpose << ": " << pending << '\n' << std::flush;
    for (unsigned links = 0; links <= 40; ++links) {
        fs::path walked = pending.root_path();
        bool expanded = false;
        for (auto part = pending.begin(); part != pending.end(); ++part) {
            if (*part == pending.root_path())
                continue;
            walked /= *part;
            refuse_protected_name(walked);
            std::error_code error;
            const auto state = fs::symlink_status(walked, error);
            if (error == std::errc::no_such_file_or_directory)
                continue;
            require(!error, "cannot inspect named path: " + walked.string());
            if (!fs::is_symlink(state))
                continue;
            const auto target = fs::read_symlink(walked);
            refuse_protected_name(target);
            auto next = target.is_absolute() ? target : walked.parent_path() / target;
            for (auto rest = std::next(part); rest != pending.end(); ++rest)
                next /= *rest;
            refuse_protected_name(next);
            pending = next.lexically_normal();
            refuse_protected_name(pending);
            std::cout << "FILES symlink target: " << pending << '\n' << std::flush;
            expanded = true;
            break;
        }
        if (!expanded)
            return pending;
    }
    throw std::runtime_error("too many symlinks in named test path");
}

fs::path private_directory(const fs::path& base) {
    const auto pattern = checked_path(base / "real-helper-XXXXXX", "private directory write");
    const std::string text = pattern.string();
    std::vector<char> name(text.begin(), text.end());
    name.push_back('\0');
    require(::mkdtemp(name.data()) != nullptr, "cannot create assigned private test directory");
    return fs::path(name.data());
}

std::string read_bytes(const fs::path& path) {
    const auto checked = checked_path(path, "read");
    std::ifstream input(checked, std::ios::binary);
    require(input.is_open(), "cannot open test input: " + checked.string());
    std::string bytes{std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
    require(!input.bad(), "cannot read test input: " + checked.string());
    return bytes;
}

std::int64_t minute(std::int64_t offset) { return kBase + offset * kMinute; }

std::vector<pf_bar_t> bars() {
    std::vector<pf_bar_t> result;
    for (std::int64_t offset = 0; offset < 80; ++offset)
        result.push_back({100.0, 102.0, 98.0, 101.0, 10.0, minute(offset)});
    return result;
}

pfh::PlanRequest request() {
    return {minute(30), minute(45), minute(10), 2, "1", "5", "UTC", "UTC", "", true};
}

Raw empty_plan(std::int64_t start) {
    Raw result{};
    result.struct_size = 248;
    result.version = 1;
    result.bound = -1;
    result.trim_start_ms = start;
    result.shortfall = 1;  // Both empty fixtures request two pre-roll groups.
    return result;
}

Raw first_plan() {
    // F=10, T=30, E=45 minutes. Supplied opens 10,15,20,25,30,35,40: seven.
    // Four pre-T groups; N=2 keeps 20,25, trims 10 rows, feeds 25 rows and five groups.
    // The 15 selected rows seal three groups. No expected value comes from a helper.
    Raw result{};
    result.struct_size = 248;
    result.version = 1;
    result.bound = -1;
    result.supplied_input_bars = 35;
    result.supplied_script_bars = 7;
    result.available_script_bars = 4;
    result.used_script_bars = 2;
    result.trimmed_script_bars = 2;
    result.trim_index = 10;
    result.fed_input_bars = 25;
    result.fed_script_bars = 5;
    result.preroll_input_bars = 10;
    result.window_input_bars = 15;
    result.window_script_bars = 3;
    result.trim_start_ms = minute(20);
    result.preroll_first_bar_ms = minute(20);
    result.preroll_last_bar_ms = minute(25);
    result.supplied_first_data_ms = minute(10);
    result.supplied_last_data_ms = minute(44);
    result.fed_first_data_ms = minute(20);
    result.fed_last_data_ms = minute(44);
    result.window_first_data_ms = minute(30);
    result.window_last_data_ms = minute(44);
    result.present_mask = 255;
    return result;
}

Raw second_plan() {
    // F=35, T=55, E=70. Seven opens 35..65; four pre-T groups, N=3 keeps 40..50.
    // Trim five rows; feed 30 rows/six groups, split 15/15 rows and 3/3 script groups.
    Raw result{};
    result.struct_size = 248;
    result.version = 1;
    result.bound = -1;
    result.supplied_input_bars = 35;
    result.supplied_script_bars = 7;
    result.available_script_bars = 4;
    result.used_script_bars = 3;
    result.trimmed_script_bars = 1;
    result.trim_index = 5;
    result.fed_input_bars = 30;
    result.fed_script_bars = 6;
    result.preroll_input_bars = 15;
    result.window_input_bars = 15;
    result.window_script_bars = 3;
    result.trim_start_ms = minute(40);
    result.preroll_first_bar_ms = minute(40);
    result.preroll_last_bar_ms = minute(50);
    result.supplied_first_data_ms = minute(35);
    result.supplied_last_data_ms = minute(69);
    result.fed_first_data_ms = minute(40);
    result.fed_last_data_ms = minute(69);
    result.window_first_data_ms = minute(55);
    result.window_last_data_ms = minute(69);
    result.present_mask = 255;
    return result;
}

void compare_raw(const Raw& actual, const Raw& expected) {
    // Field-by-field comparison: neither padding nor a subset can hide a changed result.
#define PFH_EXPECT_FIELD(field) \
    require(actual.field == expected.field, "raw field differs: " #field)
    PFH_EXPECT_FIELD(struct_size);
    PFH_EXPECT_FIELD(version);
    PFH_EXPECT_FIELD(status);
    PFH_EXPECT_FIELD(bound);
    PFH_EXPECT_FIELD(value_ms);
    PFH_EXPECT_FIELD(previous_boundary_ms);
    PFH_EXPECT_FIELD(next_boundary_ms);
    PFH_EXPECT_FIELD(supplied_input_bars);
    PFH_EXPECT_FIELD(supplied_script_bars);
    PFH_EXPECT_FIELD(available_script_bars);
    PFH_EXPECT_FIELD(used_script_bars);
    PFH_EXPECT_FIELD(trimmed_script_bars);
    PFH_EXPECT_FIELD(trim_index);
    PFH_EXPECT_FIELD(fed_input_bars);
    PFH_EXPECT_FIELD(fed_script_bars);
    PFH_EXPECT_FIELD(preroll_input_bars);
    PFH_EXPECT_FIELD(window_input_bars);
    PFH_EXPECT_FIELD(window_script_bars);
    PFH_EXPECT_FIELD(trim_start_ms);
    PFH_EXPECT_FIELD(preroll_first_bar_ms);
    PFH_EXPECT_FIELD(preroll_last_bar_ms);
    PFH_EXPECT_FIELD(supplied_first_data_ms);
    PFH_EXPECT_FIELD(supplied_last_data_ms);
    PFH_EXPECT_FIELD(fed_first_data_ms);
    PFH_EXPECT_FIELD(fed_last_data_ms);
    PFH_EXPECT_FIELD(window_first_data_ms);
    PFH_EXPECT_FIELD(window_last_data_ms);
    PFH_EXPECT_FIELD(present_mask);
    PFH_EXPECT_FIELD(shortfall);
    PFH_EXPECT_FIELD(complete_pending_preroll_at_horizon);
    PFH_EXPECT_FIELD(reserved);
#undef PFH_EXPECT_FIELD
    require(std::equal(std::begin(actual.option), std::end(actual.option),
                       std::begin(expected.option)), "raw option differs");
}

void compare_counts(const pfh::SelectedExpectedCounts& actual,
                    const pfh::SelectedExpectedCounts& expected) {
    require(actual.fed_input_bars == expected.fed_input_bars, "expected fed input differs");
    require(actual.fed_script_bars == expected.fed_script_bars, "expected fed script differs");
    require(actual.preroll_input_bars == expected.preroll_input_bars, "expected pre input differs");
    require(actual.preroll_script_bars == expected.preroll_script_bars,
            "expected pre script differs");
    require(actual.window_input_bars == expected.window_input_bars,
            "expected window input differs");
    require(actual.window_script_bars == expected.window_script_bars,
            "expected window script differs");
}

template <typename Function>
void expect_invariant(const char* diagnostic, Function function) {
    try {
        function();
    } catch (const pfh::TypedHpoError<std::runtime_error>& error) {
        require(error.code() == "hpo_invariant" && error.origin() == pfh::FailureOrigin::Hpo &&
                    error.args().empty(), "wrong invariant metadata");
        require(std::string(error.what()).find(diagnostic) != std::string::npos,
                "wrong invariant field/rule: " + std::string(error.what()));
        return;
    }
    throw std::runtime_error("missing invariant: " + std::string(diagnostic));
}

template <typename Function>
void expect_refusal(const char* status, const char* option,
                    std::initializer_list<std::string> diagnostics, Function function) {
    try {
        function();
    } catch (const pfh::TypedHpoError<std::invalid_argument>& error) {
        require(error.code() == "hpo_walkforward_plan_refused", "wrong refusal code");
        require(error.origin() == pfh::FailureOrigin::Hpo && error.args().size() == 2,
                "wrong refusal origin or extra catalog arguments");
        require(std::get<std::string>(error.args().at("status").value()) == status,
                "wrong closed status argument");
        require(std::get<std::string>(error.args().at("option").value()) == option,
                "wrong closed option argument");
        for (const auto& text : diagnostics)
            require(std::string(error.what()).find(text) != std::string::npos,
                    "missing offending value: " + text + " in " + error.what());
        return;
    }
    throw std::runtime_error("planner refusal was swallowed: " + std::string(status));
}

template <typename Mutation>
void invalid_plan(const char* name, const char* diagnostic, Mutation mutation) {
    row(name, [&] {
        auto raw = first_plan();
        auto wanted = request();
        mutation(raw, wanted);
        const auto owner = std::make_shared<const pfh::Dataset>(bars());
        const pfh::DatasetView bounded(owner, 10, 35);
        expect_invariant(diagnostic, [&] { Access::validate({raw}, bounded, wanted); });
    });
}

void unit_rows() {
    row("unit-hand-built-valid-plan", [] {
        // Proves the seam accepts a valid independently built plan; false pass: always throw.
        const auto owner = std::make_shared<const pfh::Dataset>(bars());
        const pfh::DatasetView bounded(owner, 10, 35);
        Access::validate({first_plan()}, bounded, request());
        Access::check_status({first_plan()}, request());
    });
    // Each equality row preserves every other equality and timestamp rule. Removing its
    // named check must accept the mutation, not fail incidentally on another count.
    invalid_plan("unit-supplied-size", "supplied_input_bars differs from bounded.size()",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Keep supplied = trim + fed; point fed_first at the changed trim index.
        raw.supplied_input_bars = 36;
        raw.trim_index = 11;
        raw.fed_first_data_ms = minute(21);
    });
    invalid_plan("unit-retained-tail", "fed_input_bars differs from supplied_input_bars",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Keep fed = pre + window; only the supplied/trim/fed equality breaks.
        raw.fed_input_bars = 26;
        raw.preroll_input_bars = 11;
    });
    invalid_plan("unit-input-partition", "window_input_bars differs from fed_input_bars",
                 [](Raw& raw, pfh::PlanRequest&) {
        // The window count still matches the tape, and the retained tail is unchanged.
        raw.preroll_input_bars = 9;
    });
    invalid_plan("unit-script-partition", "window_script_bars differs from fed_script_bars",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Only fed_script = used + window_script breaks; six is still below 25 rows.
        raw.fed_script_bars = 6;
    });
    invalid_plan("unit-supplied-script-partition", "supplied_script_bars differs from available",
                 [](Raw& raw, pfh::PlanRequest&) {
        // All retained counts stay valid; only supplied_script = available + window breaks.
        raw.supplied_script_bars = 8;
    });
    invalid_plan("unit-used-minimum", "used_script_bars differs from min(",
                 [](Raw&, pfh::PlanRequest& wanted) {
        // N=3 still has no shortfall with four available, but used=2 is no longer min(N,A).
        wanted.preroll_bars = 3;
    });
    invalid_plan("unit-trimmed-script-partition", "trimmed_script_bars differs from available",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Only trimmed = available - used breaks; no row or timestamp changes.
        raw.trimmed_script_bars = 3;
    });
    invalid_plan("unit-shortfall-meaning", "shortfall disagrees with available",
                 [](Raw& raw, pfh::PlanRequest&) {
        // A valid Boolean with the wrong meaning must fail even after Boolean admission.
        raw.shortfall = 1;
    });
    invalid_plan("unit-window-row-count", "window_input_bars differs from the number",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Preserve fed = pre + window; only the independently searched window count breaks.
        raw.window_input_bars = 14;
        raw.preroll_input_bars = 11;
    });
    invalid_plan("unit-trim-open", "trim_start_ms differs from the first kept",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Only trim_start's equality to the kept group's label breaks.
        raw.trim_start_ms = minute(21);
    });
    invalid_plan("unit-supplied-first-row", "supplied_first_data_ms differs from the first",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Ordered, in range and before fed/window; only the exact supplied endpoint breaks.
        raw.supplied_first_data_ms = minute(11);
    });
    invalid_plan("unit-supplied-last-row", "supplied_last_data_ms differs from the last",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Within [F,E) and ordered; only the exact supplied endpoint breaks.
        raw.supplied_last_data_ms = minute(43);
    });
    invalid_plan("unit-fed-first-row", "fed_first_data_ms differs from the row at trim_index",
                 [](Raw& raw, pfh::PlanRequest&) {
        // The script label can precede the first input; only the exact fed endpoint breaks.
        raw.fed_first_data_ms = minute(21);
    });
    invalid_plan("unit-fed-last-row", "fed_last_data_ms differs from the last",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Permit a later in-range fed endpoint; window stays inside it.
        raw.fed_last_data_ms = minute(44) + 1;
    });
    invalid_plan("unit-window-first-row", "window_first_data_ms differs from the first",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Still >= T and inside fed; only the exact first window endpoint breaks.
        raw.window_first_data_ms = minute(31);
    });
    invalid_plan("unit-window-last-row", "window_last_data_ms differs from the last",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Still inside fed; only the exact last window endpoint breaks.
        raw.window_last_data_ms = minute(43);
    });
    invalid_plan("unit-status-not-ok", "status must be Ok",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Raw validation requires success; false pass: it validates refusal counts as a plan.
        raw.status = 6;
    });
    invalid_plan("unit-trim-overflow-guard", "trim_index exceeds supplied_input_bars",
                 [](Raw& raw, pfh::PlanRequest&) {
        // A guard, not an equality: reject before subtracting or indexing this huge offset.
        raw.trim_index = std::numeric_limits<std::uint64_t>::max();
    });
    invalid_plan("unit-preroll-input-guard", "preroll_input_bars exceeds fed_input_bars",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Guard subtraction; false pass: an unsigned wrap hides an oversized pre-roll.
        raw.preroll_input_bars = std::numeric_limits<std::uint64_t>::max();
    });
    invalid_plan("unit-used-script-guard", "used_script_bars exceeds fed_script_bars",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Guard subtraction; false pass: a wrapped script partition looks consistent.
        raw.used_script_bars = std::numeric_limits<std::uint64_t>::max();
    });
    invalid_plan("unit-available-script-guard", "available_script_bars exceeds supplied",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Guard subtraction before deriving supplied-window counts.
        raw.available_script_bars = std::numeric_limits<std::uint64_t>::max();
    });
    invalid_plan("unit-fed-script-row-bound", "fed_script_bars exceeds retained input rows",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Preserve both script partitions, used/min and trimming. Only groups <= rows breaks.
        raw.fed_script_bars = 26;
        raw.window_script_bars = 24;
        raw.supplied_script_bars = 28;
    });
    invalid_plan("unit-shortfall-boolean", "shortfall must be exactly 0 or 1",
                 [](Raw& raw, pfh::PlanRequest&) {
        // The seam repeats helper Boolean admission; false pass: every nonzero means true.
        raw.shortfall = 2;
    });
    invalid_plan("unit-pending-boolean", "complete_pending_preroll_at_horizon must be exactly",
                 [](Raw& raw, pfh::PlanRequest&) {
        // False pass: a malformed flag is silently preserved as caller policy.
        raw.complete_pending_preroll_at_horizon = 2;
    });
    invalid_plan("unit-preroll-label-order", "preroll_first_bar_ms is after",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Keep trim_start equal to the first label; only label ordering breaks.
        raw.preroll_first_bar_ms = minute(26);
        raw.trim_start_ms = minute(26);
    });
    invalid_plan("unit-preroll-label-below-F", "preroll_first_bar_ms is below F",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Keep trim_start equal and labels ordered; only the F range check breaks.
        raw.preroll_first_bar_ms = minute(9);
        raw.trim_start_ms = minute(9);
    });
    invalid_plan("unit-preroll-label-at-T", "preroll_last_bar_ms is at or after T",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Other masks/endpoints/equalities hold; pre-roll labels must precede T.
        raw.preroll_last_bar_ms = minute(30);
    });
    invalid_plan("unit-supplied-at-E", "supplied_last_data_ms is at or after E",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Guard range before exact endpoint checking; false pass: E becomes inclusive.
        raw.supplied_last_data_ms = minute(45);
    });
    invalid_plan("unit-window-before-T", "window_first_data_ms is below T",
                 [](Raw& raw, pfh::PlanRequest&) {
        // Guard window membership before exact endpoint checking.
        raw.window_first_data_ms = minute(29);
    });
    row("unit-each-nonempty-timestamp-bit", [] {
        // Proves every present bit is tied to the right count. False pass: check only a pair's
        // first bit, or use window_script (zero for an unsealed tail) instead of window_input.
        const char* const names[]{
            "preroll_first_bar_ms", "preroll_last_bar_ms", "supplied_first_data_ms",
            "supplied_last_data_ms", "fed_first_data_ms", "fed_last_data_ms",
            "window_first_data_ms", "window_last_data_ms"};
        const auto owner = std::make_shared<const pfh::Dataset>(bars());
        const pfh::DatasetView bounded(owner, 10, 35);
        for (unsigned bit = 0; bit < 8; ++bit) {
            auto raw = first_plan();
            raw.present_mask &= ~(std::uint32_t{1} << bit);
            expect_invariant((std::string(names[bit]) + " present_mask").c_str(),
                             [&] { Access::validate({raw}, bounded, request()); });
        }
    });
    row("unit-zero-count-timestamp-bits", [] {
        // Proves zero-count groups have neither bit, including zero-valued real timestamps.
        // False pass: test the timestamp value instead of its present bit.
        const auto owner = std::make_shared<const pfh::Dataset>(std::vector<pf_bar_t>{});
        const pfh::DatasetView bounded(owner);
        for (unsigned bit = 0; bit < 8; ++bit) {
            auto raw = empty_plan(minute(30));
            raw.present_mask = std::uint32_t{1} << bit;
            expect_invariant("present_mask disagrees with its count",
                             [&] { Access::validate({raw}, bounded, request()); });
        }
    });
    row("unit-internal-error-empty-option", [] {
        // Internal exceptions are not safely inducible in a real calendar. This UNIT result
        // proves internal_error/none mapping; false pass: fall through to an untyped error.
        Raw raw{};
        raw.struct_size = 248;
        raw.version = 1;
        raw.status = 6;
        raw.bound = -1;
        expect_refusal("internal_error", "none", {"value_ms=0", "bound=-1 (none)",
                       "previous_boundary_ms=0", "next_boundary_ms=0"},
                       [&] { Access::check_status({raw}, request()); });
    });
    row("unit-all-documented-refusal-options", [] {
        // Only vocabulary admission is under test, not engine reachability of these pairings.
        // False pass: the converter omits an infrequent engine option such as calendar.
        for (const char* option : {"calendar", "end_ms", "engine_timezone", "fed_start_ms",
                                  "input_tf", "preroll_bars", "rows", "script_tf", "session",
                                  "start_ms", "timeframes", "timezone"}) {
            Raw raw{};
            raw.struct_size = 248;
            raw.version = 1;
            raw.status = 6;
            raw.bound = -1;
            std::copy(option, option + std::strlen(option), std::begin(raw.option));
            expect_refusal("internal_error", option, {"value_ms=0"},
                           [&] { Access::check_status({raw}, request()); });
        }
    });
    row("unit-unknown-refusal-vocabulary", [] {
        // Proves unknown engine words never escape as undeclared catalog arguments.
        // False pass: forward an arbitrary string or cast an unknown status into an enum.
        Raw raw{};
        raw.struct_size = 248;
        raw.version = 1;
        raw.status = 7;
        raw.bound = -1;
        expect_invariant("status is not a known",
                         [&] { Access::check_status({raw}, request()); });
        raw.status = 6;
        const std::string unknown = "future_option";
        std::copy(unknown.begin(), unknown.end(), std::begin(raw.option));
        expect_invariant("option is outside",
                         [&] { Access::check_status({raw}, request()); });
    });
}

void lane3_equalities(const pfh::SelectedPlan& plan) {
    // Written independently of SelectedEvaluation's validator: these are consumer constraints,
    // not predictions learned from the planner. A wrong field mapping must fail this check too.
    const auto& counts = plan.expected;
    require(counts.fed_input_bars == plan.retained.size(), "lane3: fed input != retained size");
    require(counts.preroll_input_bars <= counts.fed_input_bars, "lane3: oversized pre input");
    require(counts.window_input_bars == counts.fed_input_bars - counts.preroll_input_bars,
            "lane3: input partition differs");
    require(counts.preroll_script_bars <= counts.fed_script_bars, "lane3: oversized pre script");
    require(counts.window_script_bars == counts.fed_script_bars - counts.preroll_script_bars,
            "lane3: script partition differs");
    require(counts.fed_script_bars <= plan.retained.size(), "lane3: more groups than input rows");
}

void real_rows(const pfh::WindowPlanHelper& helper) {
    const auto owner = std::make_shared<const pfh::Dataset>(bars());
    const pfh::DatasetView tape(owner);
    const auto before = bars();
    std::vector<pfh::SelectedPlan> plans;
    row("real-golden-window", [&] {
        // Proves hand-derived numbers AND no-copy trim offset. False pass: correct counts on
        // a repacked copy, using available=4 as preroll_script, or skipping F's initial cut.
        plans.push_back(pfh::plan_selected_window(helper, tape, request()));
        const auto& plan = plans.back();
        require(plan.plan.ok() && plan.plan.status() == pfh::PlanStatus::Ok, "golden not Ok");
        compare_raw(plan.plan.raw, first_plan());
        compare_counts(plan.expected, {25, 5, 10, 2, 15, 3});
        require(plan.supplied_first_index == 10, "golden supplied owner index differs");
        require(plan.retained.data() == tape.data() + 20, "golden retained pointer copied/moved");
        require(plan.retained.begin == 20 && plan.retained.size() == 25 &&
                    plan.retained.size() == plan.plan.raw.fed_input_bars,
                "golden retained range differs");
        require(plan.retained.owner == owner, "golden lost the original owner");
    });
    row("real-bounded-cut-equivalence", [&] {
        // Proves rows before F and at/after E never reach the planner, including a nonzero
        // tape.begin and an owner prefix. False pass: whole later tape, double-added begin,
        // inclusive E, or changed raw fields hidden by comparing only the six counts.
        const std::vector<pfh::DatasetView> views{
            pfh::DatasetView(owner, 10, 35), pfh::DatasetView(owner, 0, 45),
            pfh::DatasetView(owner, 7, 60)};
        for (const auto& view : views) {
            auto plan = pfh::plan_selected_window(helper, view, request());
            compare_raw(plan.plan.raw, plans.front().plan.raw);
            compare_counts(plan.expected, plans.front().expected);
            require(plan.supplied_first_index == 10 && plan.retained.begin == 20 &&
                        plan.retained.size() == 25 && plan.retained.owner == owner &&
                        plan.retained.data() == tape.data() + 20,
                    "bounded view identity differs");
            plans.push_back(std::move(plan));
        }
    });
    row("real-fold-advance", [&] {
        // Proves a later independent F/T/E/N cut on the SAME tape. False pass: reuse the first
        // window's trim index/counts or retain from a tape-relative rather than owner index.
        auto wanted = request();
        wanted.fed_start_ms = minute(35);
        wanted.start_ms = minute(55);
        wanted.end_ms = minute(70);
        wanted.preroll_bars = 3;
        plans.push_back(pfh::plan_selected_window(helper, tape, wanted));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, second_plan());
        compare_counts(plan.expected, {30, 6, 15, 3, 15, 3});
        require(plan.supplied_first_index == 35 && plan.retained.begin == 40 &&
                    plan.retained.data() == tape.data() + 40 && plan.retained.size() == 30,
                "later fold used the earlier fold's offset");
    });
    row("real-zero-preroll", [&] {
        // Four supplied pre-T groups, N=0: discard all 20 pre-T rows, keep 30..44 (15 rows,
        // three groups). False pass: always retain one group or leave pre-roll bits present.
        auto wanted = request();
        wanted.preroll_bars = 0;
        auto expected = first_plan();
        expected.used_script_bars = 0;
        expected.trimmed_script_bars = 4;
        expected.trim_index = 20;
        expected.fed_input_bars = 15;
        expected.fed_script_bars = 3;
        expected.preroll_input_bars = 0;
        expected.trim_start_ms = minute(30);
        expected.preroll_first_bar_ms = 0;
        expected.preroll_last_bar_ms = 0;
        expected.fed_first_data_ms = minute(30);
        expected.present_mask = 252;  // Only the six input-data endpoint bits.
        plans.push_back(pfh::plan_selected_window(helper, tape, wanted));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, expected);
        compare_counts(plan.expected, {15, 3, 0, 0, 15, 3});
        require(plan.retained.data() == tape.data() + 30 && plan.retained.size() == 15,
                "zero pre-roll retained old groups");
    });
    row("real-shortfall-with-unsealed-window-tail", [&] {
        // Rows 25..31: open pre group 25 (five rows), window group 30 (two rows, unsealed).
        // N=3 > available=1: shortfall=1, no trim, seven rows but only one executed group.
        // False pass: throw on shortfall or require window_script>0 for window timestamps.
        auto wanted = request();
        wanted.preroll_bars = 3;
        Raw expected{};
        expected.struct_size = 248;
        expected.version = 1;
        expected.bound = -1;
        expected.supplied_input_bars = 7;
        expected.supplied_script_bars = 1;
        expected.available_script_bars = 1;
        expected.used_script_bars = 1;
        expected.fed_input_bars = 7;
        expected.fed_script_bars = 1;
        expected.preroll_input_bars = 5;
        expected.window_input_bars = 2;
        expected.trim_start_ms = minute(25);
        expected.preroll_first_bar_ms = minute(25);
        expected.preroll_last_bar_ms = minute(25);
        expected.supplied_first_data_ms = minute(25);
        expected.supplied_last_data_ms = minute(31);
        expected.fed_first_data_ms = minute(25);
        expected.fed_last_data_ms = minute(31);
        expected.window_first_data_ms = minute(30);
        expected.window_last_data_ms = minute(31);
        expected.present_mask = 255;
        expected.shortfall = 1;
        plans.push_back(pfh::plan_selected_window(helper, pfh::DatasetView(owner, 25, 7), wanted));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, expected);
        compare_counts(plan.expected, {7, 1, 5, 1, 2, 0});
        require(plan.supplied_first_index == 25 && plan.retained.data() == tape.data() + 25 &&
                    plan.retained.size() == 7, "shortfall changed retained input");
    });
    row("real-pending-preroll-label-precedes-first-row", [&] {
        // Rows 21..27 open groups 20 and 25; the last is pending, completed at T=30.
        // N=3, available=used=2, shortfall=1, trim=0, feed seven pre-T rows/two groups.
        // False pass: require a group's open label (20) to be >= first input timestamp (21),
        // or drop complete_pending_preroll_at_horizon from the retained plan.
        auto wanted = request();
        wanted.preroll_bars = 3;
        Raw expected{};
        expected.struct_size = 248;
        expected.version = 1;
        expected.bound = -1;
        expected.supplied_input_bars = 7;
        expected.supplied_script_bars = 2;
        expected.available_script_bars = 2;
        expected.used_script_bars = 2;
        expected.fed_input_bars = 7;
        expected.fed_script_bars = 2;
        expected.preroll_input_bars = 7;
        expected.trim_start_ms = minute(20);
        expected.preroll_first_bar_ms = minute(20);
        expected.preroll_last_bar_ms = minute(25);
        expected.supplied_first_data_ms = minute(21);
        expected.supplied_last_data_ms = minute(27);
        expected.fed_first_data_ms = minute(21);
        expected.fed_last_data_ms = minute(27);
        expected.present_mask = 63;
        expected.shortfall = 1;
        expected.complete_pending_preroll_at_horizon = 1;
        plans.push_back(pfh::plan_selected_window(helper, pfh::DatasetView(owner, 21, 7), wanted));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, expected);
        compare_counts(plan.expected, {7, 2, 7, 2, 0, 0});
        require(plan.retained.data() == tape.data() + 21 && plan.retained.size() == 7,
                "pending pre-roll was repacked or trimmed");
    });
    row("real-empty-cut", [&] {
        // F=90 is beyond all 80 tape rows. Engine decides an empty cut is Ok with shortfall.
        // False pass: fabricate success without calling the planner, dereference end, or
        // return the original tape's first pointer instead of its empty tail.
        auto wanted = request();
        wanted.fed_start_ms = minute(90);
        wanted.start_ms = minute(100);
        wanted.end_ms = minute(110);
        plans.push_back(pfh::plan_selected_window(helper, tape, wanted));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, empty_plan(minute(100)));
        compare_counts(plan.expected, {0, 0, 0, 0, 0, 0});
        require(plan.supplied_first_index == 80 && plan.retained.begin == 80 &&
                    plan.retained.data() == tape.data() + 80 && plan.retained.empty(),
                "empty cut changed its owner-relative tail");
    });
    row("real-empty-tape", [&] {
        // Proves a possibly null empty-vector pointer is never indexed. False pass: treat an
        // empty Dataset as an invalid pointer or perform null + 0 arithmetic.
        const auto empty = std::make_shared<const pfh::Dataset>(std::vector<pf_bar_t>{});
        plans.push_back(pfh::plan_selected_window(helper, pfh::DatasetView(empty), request()));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, empty_plan(minute(30)));
        compare_counts(plan.expected, {0, 0, 0, 0, 0, 0});
        require(plan.supplied_first_index == 0 && plan.retained.begin == 0 &&
                    plan.retained.data() == empty->data() && plan.retained.empty(),
                "empty owner pointer changed");
        compare_raw(helper.plan_selected_primary(nullptr, 0, request()).raw,
                    empty_plan(minute(30)));
    });
    row("real-present-zero-timestamps", [&] {
        // Unix epoch rows 0..19, F=0, T=10, E=20, N=2. Four five-minute groups, two pre-T
        // and two selected; no trim. False pass: interpret a zero timestamp as absent even
        // when its bit is set, or discard the first row because F is treated as exclusive.
        auto epoch_bars = bars();
        for (auto& bar : epoch_bars)
            bar.timestamp -= kBase;
        const auto epoch_owner = std::make_shared<const pfh::Dataset>(std::move(epoch_bars));
        const pfh::PlanRequest wanted{
            600000, 1200000, 0, 2, "1", "5", "UTC", "UTC", "", true};
        Raw expected{};
        expected.struct_size = 248;
        expected.version = 1;
        expected.bound = -1;
        expected.supplied_input_bars = 20;
        expected.supplied_script_bars = 4;
        expected.available_script_bars = 2;
        expected.used_script_bars = 2;
        expected.fed_input_bars = 20;
        expected.fed_script_bars = 4;
        expected.preroll_input_bars = 10;
        expected.window_input_bars = 10;
        expected.window_script_bars = 2;
        expected.preroll_last_bar_ms = 300000;
        expected.supplied_last_data_ms = 1140000;
        expected.fed_last_data_ms = 1140000;
        expected.window_first_data_ms = 600000;
        expected.window_last_data_ms = 1140000;
        expected.present_mask = 255;
        plans.push_back(pfh::plan_selected_window(helper, pfh::DatasetView(epoch_owner), wanted));
        const auto& plan = plans.back();
        compare_raw(plan.plan.raw, expected);
        compare_counts(plan.expected, {20, 4, 10, 2, 10, 2});
        require(plan.supplied_first_index == 0 && plan.retained.begin == 0 &&
                    plan.retained.data() == epoch_owner->data() && plan.retained.size() == 20,
                "zero-valued first timestamp changed the retained range");
    });
    row("real-owner-lifetime", [&] {
        // Proves the result alone keeps its owner alive; false pass: return a borrowed pointer.
        std::weak_ptr<const pfh::Dataset> weak;
        {
            const auto kept = [&] {
                auto local = std::make_shared<const pfh::Dataset>(bars());
                weak = local;
                return pfh::plan_selected_window(helper, pfh::DatasetView(local), request());
            }();
            require(!weak.expired() && kept.retained.data()[0].timestamp == minute(20),
                    "retained view lost its only owner");
            compare_raw(kept.plan.raw, first_plan());
            compare_counts(kept.expected, {25, 5, 10, 2, 15, 3});
            lane3_equalities(kept);
        }
        require(weak.expired(), "planning retained a hidden owner after result destruction");
    });
    row("real-every-plan-satisfies-lane3-and-tape-is-immutable", [&] {
        // Proves all golden/empty/shortfall plans meet independent consumer equalities.
        // False pass: counts agree with a hand vector but disagree with retained size or
        // a planning call edits OHLCV to make grouping work.
        require(plans.size() == 11, "a golden planning row was omitted");
        for (const auto& plan : plans)
            lane3_equalities(plan);
        require(std::memcmp(tape.data(), before.data(), before.size() * sizeof(pf_bar_t)) == 0,
                "planning modified the shared OHLCV tape");
    });
    row("real-boundary-T-unaligned", [&] {
        // T=31 lies between opens 30 and 35; false pass: snap T or omit its offending value.
        auto wanted = request();
        wanted.start_ms = minute(31);
        expect_refusal("boundary_unaligned", "start_ms",
                       {"value_ms=" + std::to_string(minute(31)), "bound=0 (T)",
                        "previous_boundary_ms=" + std::to_string(minute(30)),
                        "next_boundary_ms=" + std::to_string(minute(35))},
                       [&] { (void)pfh::plan_selected_window(helper, tape, wanted); });
    });
    row("real-boundary-E-unaligned", [&] {
        // E=44 lies between opens 40 and 45; false pass: truncate/snap E to admit the request.
        auto wanted = request();
        wanted.end_ms = minute(44);
        expect_refusal("boundary_unaligned", "end_ms",
                       {"value_ms=" + std::to_string(minute(44)), "bound=1 (E)",
                        "previous_boundary_ms=" + std::to_string(minute(40)),
                        "next_boundary_ms=" + std::to_string(minute(45))},
                       [&] { (void)pfh::plan_selected_window(helper, tape, wanted); });
    });
    row("real-boundary-F-unaligned", [&] {
        // F=11 lies between opens 10 and 15; false pass: snap F to the first supplied group.
        auto wanted = request();
        wanted.fed_start_ms = minute(11);
        expect_refusal("boundary_unaligned", "fed_start_ms",
                       {"value_ms=" + std::to_string(minute(11)), "bound=2 (F)",
                        "previous_boundary_ms=" + std::to_string(minute(10)),
                        "next_boundary_ms=" + std::to_string(minute(15))},
                       [&] { (void)pfh::plan_selected_window(helper, tape, wanted); });
    });
    row("real-request-end-before-start", [&] {
        // E=29 < T=30 is rejected at request admission. False pass: adjust E or classify it
        // as an alignment error instead of preserving the planner's admission order.
        auto wanted = request();
        wanted.end_ms = minute(29);
        expect_refusal("request_invalid", "end_ms",
                       {"value_ms=" + std::to_string(minute(29)), "bound=1 (E)",
                        "previous_boundary_ms=" + std::to_string(minute(30))},
                       [&] { (void)pfh::plan_selected_window(helper, tape, wanted); });
    });
    row("real-request-F-after-E-does-not-underflow-cut", [&] {
        // F=50 > E=45 also exceeds T=30: an empty cut must reach request admission unchanged.
        // False pass: subtract last-first as unsigned and fail in DatasetView instead.
        auto wanted = request();
        wanted.fed_start_ms = minute(50);
        expect_refusal("request_invalid", "fed_start_ms",
                       {"value_ms=" + std::to_string(minute(50)), "bound=2 (F)",
                        "next_boundary_ms=" + std::to_string(minute(30))},
                       [&] { (void)pfh::plan_selected_window(helper, tape, wanted); });
    });
    row("real-calendar-unsupported-pairing", [&] {
        // Input=60 cannot aggregate into script=5. False pass: change timeframes to obtain a
        // plan or fail on later F/T/E alignment instead of the pairing.
        auto wanted = request();
        wanted.input_tf = "60";
        expect_refusal("calendar_unsupported", "timeframes",
                       {"value_ms=0", "input_tf='60'", "script_tf='5'"},
                       [&] { (void)pfh::plan_selected_window(helper, tape, wanted); });
    });
    row("real-strict-feed-gap-refused", [&] {
        // 09:30 is in [F,E) but outside the two declared session windows.
        // False pass: feed_tolerant=false falls back to a raw interval or hides rows refusal.
        const auto at = [](int hour, int minute_of_hour) {
            return kBase + static_cast<std::int64_t>(hour * 60 + minute_of_hour) * kMinute;
        };
        const auto bar = [](std::int64_t timestamp) {
            return pf_bar_t{100.0, 102.0, 98.0, 101.0, 10.0, timestamp};
        };
        const auto strict_owner = std::make_shared<const pfh::Dataset>(
            std::vector<pf_bar_t>{
                bar(at(9, 0)), bar(at(9, 30)), bar(at(10, 0)), bar(at(10, 5))});
        const pfh::DatasetView strict_tape(strict_owner);
        auto wanted = request();
        wanted.start_ms = at(10, 0);
        wanted.end_ms = at(10, 15);
        wanted.fed_start_ms = at(9, 0);
        wanted.session = "0900-0930,1000-1030";
        wanted.feed_tolerant = false;
        expect_refusal("calendar_unsupported", "rows",
                       {"value_ms=" + std::to_string(at(9, 30))},
                       [&] {
                           (void)pfh::plan_selected_window(helper, strict_tape, wanted);
                       });
    });
    row("real-feed-range-unreachable-after-bounding", [&] {
        // The valid Dataset contains both offending kinds, but plan_selected_window excludes
        // them. Direct REAL-helper calls prove the status and its typed conversion, not a fake
        // bridge. False pass: forward the full tape or make E inclusive and swallow a refusal.
        const auto bounded = pfh::plan_selected_window(helper, tape, request());
        compare_raw(bounded.plan.raw, first_plan());
        lane3_equalities(bounded);
        const auto below = helper.plan_selected_primary(tape.data(), 1, request());
        require(below.status() == pfh::PlanStatus::FeedRangeInvalid, "below-F status differs");
        expect_refusal("feed_range_invalid", "rows",
                       {"value_ms=" + std::to_string(minute(0)), "bound=2 (F)",
                        "previous_boundary_ms=" + std::to_string(minute(10)),
                        "next_boundary_ms=" + std::to_string(minute(45))},
                       [&] { Access::check_status(below, request()); });
        const auto at_end = helper.plan_selected_primary(tape.data() + 45, 1, request());
        require(at_end.status() == pfh::PlanStatus::FeedRangeInvalid, "at-E status differs");
        expect_refusal("feed_range_invalid", "rows",
                       {"value_ms=" + std::to_string(minute(45)), "bound=1 (E)"},
                       [&] { Access::check_status(at_end, request()); });
    });
    row("real-unordered-direct-array-and-dataset-admission", [&] {
        // A descending 23,22 pair INSIDE [F,E) reaches RowsUnordered on the real helper.
        // Dataset cannot represent it, so test conversion through the explicit status seam.
        // False pass: pretend this status is reachable from a valid immutable DatasetView,
        // or report success because Dataset rejected the fixture before the planner ran.
        auto unordered = bars();
        std::swap(unordered[22], unordered[23]);
        const auto refused = helper.plan_selected_primary(unordered.data() + 10, 35, request());
        require(refused.status() == pfh::PlanStatus::RowsUnordered, "unordered status differs");
        expect_refusal("rows_unordered", "rows",
                       {"value_ms=" + std::to_string(minute(22)), "bound=-1 (none)",
                        "previous_boundary_ms=" + std::to_string(minute(23))},
                       [&] { Access::check_status(refused, request()); });
        bool dataset_refused = false;
        try {
            (void)pfh::Dataset(std::move(unordered));
        } catch (const pfh::TypedHpoError<std::invalid_argument>& error) {
            dataset_refused = true;
            require(error.code() == "hpo_dataset_invalid", "wrong unordered Dataset refusal");
        }
        require(dataset_refused, "Dataset admitted unordered rows; binary cut premise broke");
    });
    row("real-helper-failure-propagates", [&] {
        // Embedded NUL is rejected by the loaded helper's wrapper before its bridge call.
        // False pass: translate an infrastructure error into a planner request refusal.
        auto wanted = request();
        wanted.script_tf = std::string("5\0x", 3);
        bool caught = false;
        try {
            (void)pfh::plan_selected_window(helper, tape, wanted);
        } catch (const pfh::TypedHpoError<std::runtime_error>& error) {
            caught = true;
            require(error.code() == "hpo_walkforward_unavailable" && error.args().size() == 1 &&
                        std::get<std::string>(error.args().at("reason").value()) ==
                            "window_plan_load" &&
                        std::string(error.what()).find("embedded NUL") != std::string::npos,
                    "helper failure was reclassified or replaced");
        }
        require(caught, "helper failure was swallowed");
    });
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: test_selected_planning ASSIGNED_RUN_DIRECTORY");
        const auto base = checked_path(argv[1], "assigned run root write");
        const char* helper_env = std::getenv("PFH_WINDOW_PLAN_LIB");
        const char* required_env = std::getenv("PFH_REQUIRE_WINDOW_PLAN");
        const bool required = required_env != nullptr && std::string(required_env) == "1";
        fs::path helper_path;
        if (helper_env != nullptr && helper_env[0] != '\0') {
            require(fs::path(helper_env).is_absolute(), "PFH_WINDOW_PLAN_LIB must be absolute");
            helper_path = checked_path(helper_env, "real planner helper");
        }
        fs::create_directories(base);
        unit_rows();
        scenario = "real-helper-availability";
        if (helper_path.empty() || !fs::is_regular_file(helper_path)) {
            std::cout << "ROW real-helper UNAVAILABLE: PFH_WINDOW_PLAN_LIB is unset or missing\n";
            require(!required, "PFH_REQUIRE_WINDOW_PLAN=1 requires the built real helper");
            std::cout << "RUN.COMPLETE selected-planning UNAVAILABLE\n";
            return 0;
        }
        const auto bytes = read_bytes(helper_path);
        const auto digest = pfh::detail::sha256(bytes);
        const auto directory = private_directory(base);
        const auto helper = pfh::WindowPlanHelper::load(helper_path, digest, directory);
        real_rows(helper);
        std::cout << "RUN.COMPLETE selected-planning PASS real-helper=present\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ROW " << scenario << " FAIL: " << error.what() << '\n';
        return 1;
    }
}
