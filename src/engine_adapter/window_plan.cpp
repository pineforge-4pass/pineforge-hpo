#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/window_plan.hpp>

// Selected/observer declarations are used only for consumer ABI pins in this file.
#include <pineforge/execution_observer.h>
#include <pineforge/selected_window.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <initializer_list>
#include <iterator>
#include <random>
#include <stdexcept>
#include <system_error>
#include <type_traits>
#include <utility>

#if defined(__unix__) || defined(__APPLE__)
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

namespace pineforge::hpo {
namespace {

// Literal consumer pins from engine src/c_abi.cpp:1315-1360,
// src/selected_window_plan_c.cpp:33-76 and tests/test_selected_window_plan_c.cpp:73-156.
// These assert the ENGINE declarations, never a locally defined mirror.
#define PFH_PLAN_OFFSET(type, field, value) \
    static_assert(offsetof(type, field) == value, #type "::" #field " offset moved")

#if UINTPTR_MAX == UINT64_MAX
static_assert(sizeof(pf_selected_window_config_v1) == 24, "selected config size moved");
static_assert(sizeof(pf_selected_window_counts_v1) == 80, "selected counts size moved");
static_assert(sizeof(pf_execution_boundary_v1) == 24, "observer boundary size moved");
static_assert(sizeof(pf_boundary_receipt_v1) == 32, "observer receipt size moved");
static_assert(sizeof(pf_execution_observer_v1) == 24, "observer descriptor size moved");
static_assert(sizeof(pf_execution_observation_v1) == 48, "observer observation size moved");
PFH_PLAN_OFFSET(pf_selected_window_config_v1, start_ms, 8);
PFH_PLAN_OFFSET(pf_selected_window_config_v1, end_ms, 16);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, run_generation, 8);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, attempt_serial, 16);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, attempt_generation, 24);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, fed_input_bars, 32);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, fed_script_bars, 40);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, preroll_input_bars, 48);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, preroll_script_bars, 56);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, window_input_bars, 64);
PFH_PLAN_OFFSET(pf_selected_window_counts_v1, window_script_bars, 72);
PFH_PLAN_OFFSET(pf_execution_boundary_v1, run_generation, 8);
PFH_PLAN_OFFSET(pf_execution_boundary_v1, attempt_serial, 16);
PFH_PLAN_OFFSET(pf_boundary_receipt_v1, run_generation, 8);
PFH_PLAN_OFFSET(pf_boundary_receipt_v1, frame_bytes, 16);
PFH_PLAN_OFFSET(pf_boundary_receipt_v1, handed_bytes, 20);
PFH_PLAN_OFFSET(pf_boundary_receipt_v1, export_requested, 24);
PFH_PLAN_OFFSET(pf_boundary_receipt_v1, reserved, 28);
PFH_PLAN_OFFSET(pf_execution_observer_v1, context, 8);
PFH_PLAN_OFFSET(pf_execution_observer_v1, before_results, 16);
PFH_PLAN_OFFSET(pf_execution_observation_v1, run_generation, 8);
PFH_PLAN_OFFSET(pf_execution_observation_v1, attempt_serial, 16);
PFH_PLAN_OFFSET(pf_execution_observation_v1, attempt_generation, 24);
PFH_PLAN_OFFSET(pf_execution_observation_v1, phase, 32);
PFH_PLAN_OFFSET(pf_execution_observation_v1, fault_stage, 36);
PFH_PLAN_OFFSET(pf_execution_observation_v1, attempt_outcome, 40);
PFH_PLAN_OFFSET(pf_execution_observation_v1, boundary_delivered, 44);
static_assert(sizeof(pf_selected_plan_request_v1) == 80, "planner request size moved");
static_assert(alignof(pf_selected_plan_request_v1) == 8, "planner request alignment moved");
static_assert(sizeof(pf_selected_plan_result_v1) == 248, "planner result size moved");
static_assert(alignof(pf_selected_plan_result_v1) == 8, "planner result alignment moved");
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, struct_size, 0);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, version, 4);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, start_ms, 8);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, end_ms, 16);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, fed_start_ms, 24);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, preroll_bars, 32);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, feed_tolerant, 36);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, input_tf, 40);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, script_tf, 48);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, chart_timezone, 56);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, engine_timezone, 64);
PFH_PLAN_OFFSET(pf_selected_plan_request_v1, session, 72);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, struct_size, 0);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, version, 4);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, status, 8);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, bound, 12);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, value_ms, 16);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, previous_boundary_ms, 24);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, next_boundary_ms, 32);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, supplied_input_bars, 40);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, supplied_script_bars, 48);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, available_script_bars, 56);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, used_script_bars, 64);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, trimmed_script_bars, 72);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, trim_index, 80);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, fed_input_bars, 88);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, fed_script_bars, 96);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, preroll_input_bars, 104);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, window_input_bars, 112);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, window_script_bars, 120);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, trim_start_ms, 128);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, preroll_first_bar_ms, 136);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, preroll_last_bar_ms, 144);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, supplied_first_data_ms, 152);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, supplied_last_data_ms, 160);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, fed_first_data_ms, 168);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, fed_last_data_ms, 176);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, window_first_data_ms, 184);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, window_last_data_ms, 192);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, present_mask, 200);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, shortfall, 204);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, complete_pending_preroll_at_horizon, 208);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, reserved, 212);
PFH_PLAN_OFFSET(pf_selected_plan_result_v1, option, 216);
#endif
static_assert(sizeof(double) == 8 && sizeof(std::int64_t) == 8, "bar scalar sizes moved");
static_assert(sizeof(pf_bar_t) == 48, "bar size moved");
PFH_PLAN_OFFSET(pf_bar_t, open, 0);
PFH_PLAN_OFFSET(pf_bar_t, high, 8);
PFH_PLAN_OFFSET(pf_bar_t, low, 16);
PFH_PLAN_OFFSET(pf_bar_t, close, 24);
PFH_PLAN_OFFSET(pf_bar_t, volume, 32);
PFH_PLAN_OFFSET(pf_bar_t, timestamp, 40);
static_assert(sizeof(pf_selected_plan_result_v1::option) == 32, "option size moved");
static_assert(std::is_standard_layout_v<pf_selected_plan_request_v1> &&
                  std::is_trivially_copyable_v<pf_selected_plan_request_v1>,
              "planner request must be plain data");
static_assert(std::is_standard_layout_v<pf_selected_plan_result_v1> &&
                  std::is_trivially_copyable_v<pf_selected_plan_result_v1>,
              "planner result must be plain data");
static_assert(sizeof(PlanResult) == sizeof(pf_selected_plan_result_v1),
              "typed result must retain the entire raw result");
static_assert(std::is_same_v<decltype(&::pf_selected_plan_version), std::uint32_t (*)()>,
              "planner version signature moved");
static_assert(std::is_same_v<decltype(&::pf_plan_selected_primary_v1),
                            int (*)(const pf_selected_plan_request_v1*, const pf_bar_t*,
                                    std::uint64_t, pf_selected_plan_result_v1*)>,
              "planner call signature moved");
#undef PFH_PLAN_OFFSET

[[noreturn]] void unavailable(const char* reason, const std::string& message) {
    throw TypedHpoError<std::runtime_error>(
        "hpo_walkforward_unavailable", {{"reason", reason}}, message);
}

bool valid_digest(const std::string& digest) {
    return digest.size() == 64 &&
        std::all_of(digest.begin(), digest.end(), [](char ch) {
            return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
        });
}

#if defined(__unix__) || defined(__APPLE__)

// Incremental form of the repository's SHA-256 arithmetic (src/core/sha256.hpp).
// Hashes exactly the bytes written, with bounded storage independent of helper size.
class SnapshotSha256 {
public:
    void update(const unsigned char* data, std::size_t count) {
        bytes_ += count;
        while (count != 0) {
            const auto take = std::min(count, buffer_.size() - used_);
            std::memcpy(buffer_.data() + used_, data, take);
            used_ += take;
            data += take;
            count -= take;
            if (used_ == buffer_.size()) {
                compress();
                used_ = 0;
            }
        }
    }

    std::string finish() {
        const std::uint64_t bits = bytes_ * 8;
        buffer_[used_++] = 0x80;
        if (used_ > 56) {
            std::fill(buffer_.begin() + used_, buffer_.end(), 0);
            compress();
            used_ = 0;
        }
        std::fill(buffer_.begin() + used_, buffer_.begin() + 56, 0);
        for (unsigned index = 0; index < 8; ++index)
            buffer_[56 + index] = static_cast<unsigned char>(bits >> (56 - 8 * index));
        compress();
        constexpr char hex[] = "0123456789abcdef";
        std::string result;
        result.reserve(64);
        for (const auto word : state_) {
            for (int shift = 28; shift >= 0; shift -= 4)
                result.push_back(hex[(word >> shift) & 15u]);
        }
        return result;
    }

private:
    static std::uint32_t rotate(std::uint32_t word, unsigned shift) {
        return (word >> shift) | (word << (32 - shift));
    }

    void compress() {
        constexpr std::array<std::uint32_t, 64> constants{
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
            0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
            0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
            0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
            0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
            0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
            0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
            0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
            0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
            0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
            0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
        std::array<std::uint32_t, 64> words{};
        for (std::size_t index = 0; index < 16; ++index) {
            for (std::size_t byte = 0; byte < 4; ++byte)
                words[index] = (words[index] << 8) | buffer_[index * 4 + byte];
        }
        for (std::size_t index = 16; index < words.size(); ++index) {
            const auto first = words[index - 15];
            const auto second = words[index - 2];
            words[index] = words[index - 16] +
                (rotate(first, 7) ^ rotate(first, 18) ^ (first >> 3)) + words[index - 7] +
                (rotate(second, 17) ^ rotate(second, 19) ^ (second >> 10));
        }
        auto working = state_;
        for (std::size_t index = 0; index < words.size(); ++index) {
            const auto choose = (working[4] & working[5]) ^ (~working[4] & working[6]);
            const auto majority = (working[0] & working[1]) ^ (working[0] & working[2]) ^
                (working[1] & working[2]);
            const auto first = working[7] + choose + constants[index] + words[index] +
                (rotate(working[4], 6) ^ rotate(working[4], 11) ^ rotate(working[4], 25));
            const auto second = majority +
                (rotate(working[0], 2) ^ rotate(working[0], 13) ^ rotate(working[0], 22));
            for (std::size_t slot = 7; slot > 0; --slot)
                working[slot] = working[slot - 1];
            working[4] += first;
            working[0] = first + second;
        }
        for (std::size_t index = 0; index < state_.size(); ++index)
            state_[index] += working[index];
    }

    std::array<std::uint32_t, 8> state_{
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    std::array<unsigned char, 64> buffer_{};
    std::size_t used_ = 0;
    std::uint64_t bytes_ = 0;
};

class FileDescriptor {
public:
    explicit FileDescriptor(int value) : value_(value) {}
    ~FileDescriptor() {
        if (value_ >= 0)
            ::close(value_);
    }
    FileDescriptor(const FileDescriptor&) = delete;
    FileDescriptor& operator=(const FileDescriptor&) = delete;
    int get() const noexcept { return value_; }
    void close_checked() {
        const int value = std::exchange(value_, -1);
        // Do not retry close after EINTR: the descriptor can already have been released.
        if (::close(value) != 0)
            unavailable("window_plan_load", "cannot close planner snapshot writer");
    }

private:
    int value_;
};

[[noreturn]] void io_failure(const char* operation, const std::filesystem::path& path) {
    const int error = errno;
    unavailable("window_plan_load", std::string(operation) + " '" + path.string() + "': " +
                    std::generic_category().message(error));
}

struct stat file_identity(int descriptor, const std::filesystem::path& path) {
    struct stat identity{};
    if (::fstat(descriptor, &identity) != 0)
        io_failure("cannot inspect planner file", path);
    return identity;
}

bool same_source(const struct stat& before, const struct stat& after) {
#if defined(__APPLE__)
    const auto before_mtime = before.st_mtimespec;
    const auto after_mtime = after.st_mtimespec;
    const auto before_ctime = before.st_ctimespec;
    const auto after_ctime = after.st_ctimespec;
#else
    const auto before_mtime = before.st_mtim;
    const auto after_mtime = after.st_mtim;
    const auto before_ctime = before.st_ctim;
    const auto after_ctime = after.st_ctim;
#endif
    return before.st_dev == after.st_dev && before.st_ino == after.st_ino &&
        before.st_size == after.st_size &&
        before_mtime.tv_sec == after_mtime.tv_sec &&
        before_mtime.tv_nsec == after_mtime.tv_nsec &&
        before_ctime.tv_sec == after_ctime.tv_sec &&
        before_ctime.tv_nsec == after_ctime.tv_nsec;
}

void require_private_directory(const struct stat& directory) {
    if (!S_ISDIR(directory.st_mode) || directory.st_uid != ::geteuid() ||
        (directory.st_mode & 0077) != 0) {
        unavailable("window_plan_load",
                    "planner snapshot directory must be private and user-owned");
    }
}

void require_safe_ancestry(const std::filesystem::path& directory_path) {
    const auto owner = ::geteuid();
    auto ancestor = directory_path.parent_path();
    while (!ancestor.empty()) {
        struct stat identity{};
        if (::stat(ancestor.c_str(), &identity) != 0)
            io_failure("cannot inspect planner snapshot ancestry", ancestor);
        if (!S_ISDIR(identity.st_mode) || (identity.st_uid != 0 && identity.st_uid != owner) ||
            ((identity.st_mode & 0022) != 0 && (identity.st_mode & S_ISVTX) == 0)) {
            unavailable(
                "window_plan_load",
                "planner snapshot ancestry is unsafe: path='" + ancestor.string() +
                    "', mode=" +
                    std::to_string(static_cast<unsigned long>(identity.st_mode & 07777)) +
                    ", uid=" +
                    std::to_string(static_cast<unsigned long>(identity.st_uid)));
        }
        const auto parent = ancestor.parent_path();
        if (parent == ancestor)
            break;
        ancestor = parent;
    }
}

std::string snapshot_name() {
    std::random_device random;
    std::string name = ".window-plan";
    for (int word = 0; word < 4; ++word)
        name += "-" + std::to_string(random());
#if defined(__APPLE__)
    return name + ".dylib";
#else
    return name + ".so";
#endif
}

std::string copy_and_hash(int source, int snapshot, const struct stat& before,
                          const std::filesystem::path& source_path,
                          const std::filesystem::path& snapshot_path) {
    SnapshotSha256 hash;
    std::array<unsigned char, 64 * 1024> buffer{};
    const auto size = static_cast<std::uint64_t>(before.st_size);
    std::uint64_t copied = 0;
    while (copied < size) {
        const auto wanted = static_cast<std::size_t>(
            std::min<std::uint64_t>(buffer.size(), size - copied));
        const auto received = ::read(source, buffer.data(), wanted);
        if (received < 0) {
            if (errno == EINTR)
                continue;
            io_failure("cannot read planner source", source_path);
        }
        if (received == 0)
            unavailable("window_plan_changed", "planner source shrank while snapshotting");
        const auto count = static_cast<std::size_t>(received);
        std::size_t written = 0;
        while (written < count) {
            const auto result = ::write(snapshot, buffer.data() + written, count - written);
            if (result < 0 && errno == EINTR)
                continue;
            if (result < 0)
                io_failure("cannot write planner snapshot", snapshot_path);
            if (result == 0)
                unavailable("window_plan_load",
                            "short write to planner snapshot '" + snapshot_path.string() + "'");
            written += static_cast<std::size_t>(result);
        }
        hash.update(buffer.data(), count);
        copied += count;
    }
    if (!same_source(before, file_identity(source, source_path)))
        unavailable("window_plan_changed", "planner source changed while snapshotting");
    return hash.finish();
}

template <typename Function>
Function required_symbol(void* library, const char* name, const char* reason) {
    ::dlerror();
    void* const symbol = ::dlsym(library, name);
    const char* const error = ::dlerror();
    if (error != nullptr || symbol == nullptr) {
        unavailable(reason, std::string("planner helper is missing '") + name + "': " +
                        (error != nullptr ? error : "symbol resolved to null"));
    }
    return reinterpret_cast<Function>(symbol);
}

#endif

void require_result(const pf_selected_plan_result_v1& result) {
    if (result.struct_size != sizeof(result) || result.version != 1 || result.reserved != 0 ||
        result.status < 0 || result.status > 6 || result.bound < -1 || result.bound > 2 ||
        result.present_mask > 255 || result.shortfall > 1 ||
        result.complete_pending_preroll_at_horizon > 1) {
        unavailable("window_plan_load", "planner returned an invalid result descriptor");
    }
    bool terminated = false;
    for (const unsigned char byte : result.option) {
        if (byte == 0)
            terminated = true;
        else if (terminated || byte >= 128)
            unavailable("window_plan_load", "planner returned an invalid option encoding");
    }
    if (!terminated)
        unavailable("window_plan_load", "planner returned an unterminated option");
    const std::int64_t timestamps[]{
        result.preroll_first_bar_ms, result.preroll_last_bar_ms,
        result.supplied_first_data_ms, result.supplied_last_data_ms,
        result.fed_first_data_ms, result.fed_last_data_ms,
        result.window_first_data_ms, result.window_last_data_ms};
    for (unsigned bit = 0; bit < 8; ++bit) {
        if ((result.present_mask & (1u << bit)) == 0 && timestamps[bit] != 0)
            unavailable("window_plan_load", "planner returned a nonzero absent timestamp");
    }
}

}  // namespace

WindowPlanHelper WindowPlanHelper::load(const std::filesystem::path& absolute_path,
                                        const std::string& expected_sha256_hex,
                                        const std::filesystem::path& private_run_directory) {
    return load_impl(absolute_path, expected_sha256_hex, private_run_directory, {}, {});
}

WindowPlanHelper WindowPlanHelper::load_impl(
    const std::filesystem::path& absolute_path, const std::string& expected_sha256_hex,
    const std::filesystem::path& private_run_directory,
    const std::function<void()>& after_source_open,
    const std::function<void(const std::filesystem::path&)>& after_snapshot_close) {
    // No absolute(), canonical() or filesystem access may precede this source-path check.
    if (!absolute_path.is_absolute() || absolute_path.string().find('\0') != std::string::npos)
        unavailable("window_plan_path", "planner helper path must be absolute and NUL-free");
    if (!valid_digest(expected_sha256_hex))
        unavailable("window_plan_hash_mismatch", "expected planner SHA-256 must be lowercase hex");
    if (!private_run_directory.is_absolute() ||
        private_run_directory.string().find('\0') != std::string::npos) {
        unavailable("window_plan_load", "planner snapshot directory must be absolute and NUL-free");
    }
#if defined(__unix__) || defined(__APPLE__)
    try {
        std::error_code error;
        const auto directory_path = std::filesystem::canonical(private_run_directory, error);
        if (error)
            unavailable("window_plan_load", "cannot resolve planner snapshot directory: " +
                                               error.message());
        require_safe_ancestry(directory_path);
        FileDescriptor directory(::open(directory_path.c_str(),
                                        O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW));
        if (directory.get() < 0)
            io_failure("cannot open planner snapshot directory", directory_path);
        const auto directory_before = file_identity(directory.get(), directory_path);
        require_private_directory(directory_before);

        // This fd, not a reopened source pathname, supplies every snapshot byte.
        FileDescriptor source(::open(absolute_path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK));
        if (source.get() < 0)
            io_failure("cannot open planner source", absolute_path);
        const auto before = file_identity(source.get(), absolute_path);
        if (!S_ISREG(before.st_mode) || before.st_size < 0)
            unavailable("window_plan_load", "planner source must be a regular file");
        if (after_source_open)
            after_source_open();

        WindowPlanHelper helper;
        helper.source_path_ = absolute_path;
        helper.source_device_ = static_cast<std::uint64_t>(before.st_dev);
        helper.source_inode_ = static_cast<std::uint64_t>(before.st_ino);
        helper.source_size_ = static_cast<std::uint64_t>(before.st_size);
        int snapshot_fd = -1;
        do {
            const auto name = snapshot_name();
            helper.snapshot_path_ = directory_path / name;
            snapshot_fd = ::openat(directory.get(), name.c_str(),
                                   O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        } while (snapshot_fd < 0 && errno == EEXIST);
        if (snapshot_fd < 0)
            io_failure("cannot create exclusive planner snapshot", helper.snapshot_path_);
        FileDescriptor snapshot(snapshot_fd);
        helper.sha256_ = copy_and_hash(source.get(), snapshot.get(), before,
                                       absolute_path, helper.snapshot_path_);
        if (helper.sha256_ != expected_sha256_hex)
            unavailable("window_plan_hash_mismatch",
                        "planner snapshot SHA-256 differs from expected");
        if (::fchmod(snapshot.get(), 0400) != 0)
            io_failure("cannot make planner snapshot read-only", helper.snapshot_path_);
        const auto snapshot_identity = file_identity(snapshot.get(), helper.snapshot_path_);
        snapshot.close_checked();
        if (after_snapshot_close)
            after_snapshot_close(helper.snapshot_path_);

        // Confirm that the absolute load path still names the private directory and file held
        // above. The owner must keep the checked ancestry and snapshots stable for the worker.
        struct stat directory_now{};
        struct stat snapshot_now{};
        if (::stat(directory_path.c_str(), &directory_now) != 0 ||
            ::lstat(helper.snapshot_path_.c_str(), &snapshot_now) != 0) {
            io_failure("cannot inspect completed planner snapshot", helper.snapshot_path_);
        }
        require_private_directory(directory_now);
        if (directory_before.st_dev != directory_now.st_dev ||
            directory_before.st_ino != directory_now.st_ino ||
            !same_source(snapshot_identity, snapshot_now) || !S_ISREG(snapshot_now.st_mode) ||
            // Defense in depth: no row can isolate this check because chmod changes ctime.
            (snapshot_now.st_mode & 0222) != 0) {
            unavailable("window_plan_changed", "planner snapshot path changed before loading");
        }
        helper.library_ = ::dlopen(helper.snapshot_path_.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (helper.library_ == nullptr) {
            const char* const loader_error = ::dlerror();
            unavailable("window_plan_load", "cannot load verified planner snapshot: " +
                            std::string(loader_error != nullptr ? loader_error : "unknown error"));
        }
        const auto version = bind_version(helper.library_);
        helper.plan_ = bind_plan(helper.library_);
        helper.version_ = version();
        if (helper.version_ != 1)
            unavailable("window_plan_version", "planner helper does not report bridge version 1");
        helper.self_test();
        return helper;
    } catch (const TypedHpoError<std::runtime_error>&) {
        throw;
    } catch (const std::exception& error) {
        unavailable("window_plan_load", "planner snapshot failed: " + std::string(error.what()));
    }
#else
    (void)after_source_open;
    (void)after_snapshot_close;
    unavailable("window_plan_load", "planner helper loading requires a POSIX dynamic loader");
#endif
}

WindowPlanHelper::VersionFn WindowPlanHelper::bind_version(void* library) {
#if defined(__unix__) || defined(__APPLE__)
    return required_symbol<VersionFn>(library, "pf_selected_plan_version",
                                     "window_plan_missing_pf_selected_plan_version");
#else
    (void)library;
    unavailable("window_plan_load", "planner helper loading requires a POSIX dynamic loader");
#endif
}

WindowPlanHelper::PlanFn WindowPlanHelper::bind_plan(void* library) {
#if defined(__unix__) || defined(__APPLE__)
    return required_symbol<PlanFn>(library, "pf_plan_selected_primary_v1",
                                  "window_plan_missing_pf_plan_selected_primary_v1");
#else
    (void)library;
    unavailable("window_plan_load", "planner helper loading requires a POSIX dynamic loader");
#endif
}

WindowPlanHelper::~WindowPlanHelper() { close(); }

WindowPlanHelper::WindowPlanHelper(WindowPlanHelper&& other) noexcept {
    *this = std::move(other);
}

WindowPlanHelper& WindowPlanHelper::operator=(WindowPlanHelper&& other) noexcept {
    if (this != &other) {
        close();
        source_path_ = std::move(other.source_path_);
        snapshot_path_ = std::move(other.snapshot_path_);
        sha256_ = std::move(other.sha256_);
        library_ = std::exchange(other.library_, nullptr);
        plan_ = std::exchange(other.plan_, nullptr);
        version_ = std::exchange(other.version_, 0);
        source_device_ = std::exchange(other.source_device_, 0);
        source_inode_ = std::exchange(other.source_inode_, 0);
        source_size_ = std::exchange(other.source_size_, 0);
    }
    return *this;
}

void WindowPlanHelper::close() noexcept {
#if defined(__unix__) || defined(__APPLE__)
    if (library_ != nullptr)
        ::dlclose(library_);
#endif
    library_ = nullptr;
    plan_ = nullptr;
    version_ = 0;
}

PlanResult WindowPlanHelper::plan_selected_primary(const pf_bar_t* rows, std::size_t count,
                                                  const PlanRequest& request) const {
    if (plan_ == nullptr)
        unavailable("window_plan_load", "planner helper has no bound bridge");
    for (const auto* text : {&request.input_tf, &request.script_tf, &request.chart_timezone,
                             &request.engine_timezone, &request.session}) {
        if (text->find('\0') != std::string::npos)
            unavailable("window_plan_load", "planner request contains an embedded NUL");
    }
    static_assert(sizeof(std::size_t) <= sizeof(std::uint64_t), "row count must fit the C ABI");
    pf_selected_plan_request_v1 descriptor{};
    descriptor.struct_size = static_cast<std::uint32_t>(sizeof(descriptor));
    descriptor.version = 1;
    descriptor.start_ms = request.start_ms;
    descriptor.end_ms = request.end_ms;
    descriptor.fed_start_ms = request.fed_start_ms;
    descriptor.preroll_bars = request.preroll_bars;
    descriptor.feed_tolerant = request.feed_tolerant ? 1u : 0u;
    descriptor.input_tf = request.input_tf.c_str();
    descriptor.script_tf = request.script_tf.c_str();
    descriptor.chart_timezone = request.chart_timezone.c_str();
    descriptor.engine_timezone = request.engine_timezone.c_str();
    descriptor.session = request.session.c_str();
    pf_selected_plan_result_v1 result{};
    result.struct_size = static_cast<std::uint32_t>(sizeof(result));
    result.version = 1;
    const int bridge_status = plan_(&descriptor, rows, static_cast<std::uint64_t>(count), &result);
    if (bridge_status != 0) {
        unavailable("window_plan_load", "planner C bridge failed with return " +
                                           std::to_string(bridge_status));
    }
    require_result(result);
    return PlanResult{result};
}

void WindowPlanHelper::self_test() const {
    // selected-plan-utc-six-hours/v1, frozen against the engine v1.7.0 C fixtures.
    // A later planner behavior change must bump the ID, never learn expectations at load time.
    static const std::array<pf_bar_t, 360> rows = [] {
        std::array<pf_bar_t, 360> result{};
        for (std::size_t index = 0; index < result.size(); ++index) {
            const auto value = static_cast<double>(index);
            result[index] = {100 + value, 102 + value, 98 + value, 101 + value, 110 + value,
                             1704067200000LL + static_cast<std::int64_t>(index) * 60000};
        }
        return result;
    }();
    const PlanRequest request{
        1704078000000LL, 1704088800000LL, 1704067200000LL, 2,
        "1", "60", "UTC", "UTC", "", true};
    bool matches = false;
    try {
        const auto result = plan_selected_primary(rows.data(), rows.size(), request).raw;
        const bool fields_match =
            result.struct_size == 248 && result.version == 1 &&
            result.status == 0 && result.bound == -1 &&
            result.value_ms == 0 && result.previous_boundary_ms == 0 &&
            result.next_boundary_ms == 0 &&
            result.supplied_input_bars == 360 && result.supplied_script_bars == 6 &&
            result.available_script_bars == 3 && result.used_script_bars == 2 &&
            result.trimmed_script_bars == 1 && result.trim_index == 60 &&
            result.fed_input_bars == 300 && result.fed_script_bars == 5 &&
            result.preroll_input_bars == 120 && result.window_input_bars == 180 &&
            result.window_script_bars == 3 && result.trim_start_ms == 1704070800000LL &&
            result.preroll_first_bar_ms == 1704070800000LL &&
            result.preroll_last_bar_ms == 1704074400000LL &&
            result.supplied_first_data_ms == 1704067200000LL &&
            result.supplied_last_data_ms == 1704088740000LL &&
            result.fed_first_data_ms == 1704070800000LL &&
            result.fed_last_data_ms == 1704088740000LL &&
            result.window_first_data_ms == 1704078000000LL &&
            result.window_last_data_ms == 1704088740000LL &&
            result.present_mask == 255 && result.shortfall == 0 &&
            result.complete_pending_preroll_at_horizon == 0 && result.reserved == 0;
        const bool option_empty =
            std::all_of(std::begin(result.option), std::end(result.option),
                        [](char byte) { return byte == 0; });
        matches = fields_match && option_empty;
    } catch (const std::exception& error) {
        unavailable("window_plan_self_test", std::string(self_test_id()) + ": " + error.what());
    }
    if (!matches)
        unavailable("window_plan_self_test", std::string(self_test_id()) + " differs");
}

}  // namespace pineforge::hpo
