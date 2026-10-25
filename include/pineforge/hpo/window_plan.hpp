#pragma once

#include <pineforge/selected_window_plan.h>

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <string>

namespace pineforge::hpo {

namespace detail {
/// Private test access for snapshot synchronization, symbol checks and bridge unit seams.
struct WindowPlanTestAccess;
}  // namespace detail

/// Planner status words, independent of the C bridge's negative call return codes.
enum class PlanStatus : std::int32_t {
    Ok = 0,                   ///< A complete plan is available.
    RequestInvalid = 1,       ///< The planner refused a request member or row pointer.
    CalendarUnsupported = 2,  ///< The calendar, clock or timeframe pairing is unsupported.
    BoundaryUnaligned = 3,    ///< T, E or F is not a script interval boundary.
    FeedRangeInvalid = 4,     ///< A supplied timestamp is outside [F, E).
    RowsUnordered = 5,        ///< Supplied timestamps are not strictly increasing.
    InternalError = 6         ///< The planner reported an internal failure as result data.
};

/// Owned request strings and fixed-width fields for the selected primary C planner.
struct PlanRequest {
    /// Selected start T, in Unix milliseconds.
    std::int64_t start_ms = 0;
    /// Exclusive selected end E, in Unix milliseconds.
    std::int64_t end_ms = 0;
    /// Supplied-feed floor F, in Unix milliseconds.
    std::int64_t fed_start_ms = 0;
    /// Requested pre-roll script groups N; the engine admits at most 5000.
    std::uint32_t preroll_bars = 0;
    /// Explicit input timeframe token.
    std::string input_tf;
    /// Primary script timeframe token.
    std::string script_tf;
    /// Chart timezone; an empty string retains the engine default.
    std::string chart_timezone;
    /// Engine timezone; an empty string retains the engine default.
    std::string engine_timezone;
    /// Session specification; an empty string retains the engine default.
    std::string session;
    /// Enables the engine's feed-tolerant grouping.
    bool feed_tolerant = true;
};

/// Value copy of the complete 248-byte engine result, including planner refusals.
struct PlanResult {
    /// Unmodified engine descriptor; optional timestamps are governed by present_mask.
    pf_selected_plan_result_v1 raw{};

    /// Returns the validated planner status, never a bridge-level return code.
    PlanStatus status() const noexcept { return static_cast<PlanStatus>(raw.status); }
    /// Returns whether this result contains a successful plan.
    bool ok() const noexcept { return status() == PlanStatus::Ok; }
};

/// Owns a verified, privately snapshotted standalone planner library.
///
/// Each worker owns one helper and reuses it for all planning calls. Keep this object alive
/// through every call; concurrent calls borrow immutable rows and retain no caller data.
/// Destruction closes the library. Snapshot files, including failed-load snapshots, are left
/// for the assigned run directory's owner to remove after the worker exits.
class WindowPlanHelper final {
public:
    /// Snapshots and hashes an absolute source path before loading any of its code.
    ///
    /// @param absolute_path Explicit helper path; relative paths are refused before normalization.
    /// @param expected_sha256_hex Expected content SHA-256, exactly 64 lowercase hex characters.
    /// @param private_run_directory Existing absolute directory owned by the effective user,
    ///        with no group/other permissions. Its owner must keep its ancestry stable and
    ///        leave snapshots untouched throughout the worker lifetime.
    /// @throws TypedHpoError<std::runtime_error> with hpo_walkforward_unavailable and a reason
    ///         when snapshotting, identity, binding, version or the frozen self-test fails.
    static WindowPlanHelper load(const std::filesystem::path& absolute_path,
                                 const std::string& expected_sha256_hex,
                                 const std::filesystem::path& private_run_directory);

    /// Closes the library without deleting the snapshot.
    ~WindowPlanHelper();
    /// A library owner cannot be copied.
    WindowPlanHelper(const WindowPlanHelper&) = delete;
    /// A library owner cannot be copied.
    WindowPlanHelper& operator=(const WindowPlanHelper&) = delete;
    /// Transfers ownership of the library and snapshot identity.
    WindowPlanHelper(WindowPlanHelper&& other) noexcept;
    /// Closes the previous library and transfers ownership without deleting either snapshot.
    WindowPlanHelper& operator=(WindowPlanHelper&& other) noexcept;

    /// Calls the C bridge with exactly @p rows, without copying or repacking the bar array.
    ///
    /// The caller keeps rows immutable and alive until return. Statuses 1..6 are returned as
    /// data. Negative bridge returns or malformed result descriptors throw a typed runtime
    /// error with reason window_plan_load; they never become successful plans.
    PlanResult plan_selected_primary(const pf_bar_t* rows, std::size_t count,
                                     const PlanRequest& request) const;

    /// Returns the validated bridge version, 1.
    std::uint32_t version() const noexcept { return version_; }
    /// Returns the absolute source path supplied to load(), for provenance only.
    const std::filesystem::path& source_path() const noexcept { return source_path_; }
    /// Returns the device of the opened, verified source, even if its pathname was replaced.
    std::uint64_t source_device() const noexcept { return source_device_; }
    /// Returns the inode of the opened, verified source, even if its pathname was replaced.
    std::uint64_t source_inode() const noexcept { return source_inode_; }
    /// Returns the byte length of the opened, verified source.
    std::uint64_t source_size() const noexcept { return source_size_; }
    /// Returns the absolute verified snapshot path actually passed to dlopen().
    const std::filesystem::path& snapshot_path() const noexcept { return snapshot_path_; }
    /// Returns the observed SHA-256 that matched the expected content before loading.
    const std::string& sha256() const noexcept { return sha256_; }
    /// Frozen behavior identifier; a later engine behavior change must bump this identifier.
    static constexpr const char* self_test_id() noexcept {
        return "selected-plan-utc-six-hours/v1";
    }

private:
    friend struct detail::WindowPlanTestAccess;
    using VersionFn = decltype(&::pf_selected_plan_version);
    using PlanFn = decltype(&::pf_plan_selected_primary_v1);

    WindowPlanHelper() = default;
    /// Test-only synchronization hook, called after source open/fstat and before copying.
    /// Production load() always supplies an empty hook.
    static WindowPlanHelper load_impl(const std::filesystem::path& absolute_path,
                                      const std::string& expected_sha256_hex,
                                      const std::filesystem::path& private_run_directory,
                                      const std::function<void()>& after_source_open);
    static VersionFn bind_version(void* library);
    static PlanFn bind_plan(void* library);
    void self_test() const;
    void close() noexcept;

    std::filesystem::path source_path_;
    std::filesystem::path snapshot_path_;
    std::string sha256_;
    void* library_ = nullptr;
    PlanFn plan_ = nullptr;
    std::uint32_t version_ = 0;
    std::uint64_t source_device_ = 0;
    std::uint64_t source_inode_ = 0;
    std::uint64_t source_size_ = 0;
};

}  // namespace pineforge::hpo
