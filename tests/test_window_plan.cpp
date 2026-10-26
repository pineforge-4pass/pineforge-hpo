#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/window_plan.hpp>

#include "../src/core/sha256.hpp"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <variant>
#include <vector>

namespace pineforge::hpo::detail {

// Unit access only. Injected bridges prove pointer/descriptor handling, never compatibility.
struct WindowPlanTestAccess {
    using PlanFn = decltype(&::pf_plan_selected_primary_v1);

    static WindowPlanHelper synchronized_load(
        const std::filesystem::path& path, const std::string& digest,
        const std::filesystem::path& directory, const std::function<void()>& after_source_open,
        const std::function<void(const std::filesystem::path&)>& after_snapshot_close = {}) {
        return WindowPlanHelper::load_impl(path, digest, directory, after_source_open,
                                           after_snapshot_close);
    }
    static WindowPlanHelper unit_bridge(PlanFn bridge) {
        WindowPlanHelper helper;
        helper.plan_ = bridge;
        helper.version_ = 1;
        return helper;
    }
    static PlanFn raw_bridge(const WindowPlanHelper& helper) { return helper.plan_; }
    static void require_plan_export(void* library) { (void)WindowPlanHelper::bind_plan(library); }
};

}  // namespace pineforge::hpo::detail

namespace {

namespace pfh = pineforge::hpo;
namespace fs = std::filesystem;
using Access = pfh::detail::WindowPlanTestAccess;

const char* scenario = "setup";

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

// List every named file before following it. Inspect symlink targets component by component,
// refusing a protected target before resolving or opening anything beneath it.
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

fs::path private_directory(const fs::path& base, const char* label) {
    auto pattern = checked_path(base / (std::string(label) + "-XXXXXX"), "private directory write");
    std::string text = pattern.string();
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

void write_bytes(const fs::path& path, const std::string& bytes) {
    const auto checked = checked_path(path, "write");
    const int descriptor = ::open(checked.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    require(descriptor >= 0, "cannot exclusively create test input");
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto count = ::write(descriptor, bytes.data() + offset, bytes.size() - offset);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0) {
            ::close(descriptor);
            throw std::runtime_error("cannot write test input");
        }
        offset += static_cast<std::size_t>(count);
    }
    require(::close(descriptor) == 0, "cannot close test input");
}

template <typename Function>
std::string expect_reason(const char* reason, Function function,
                         const char* diagnostic = nullptr) {
    try {
        function();
    } catch (const pfh::TypedHpoError<std::runtime_error>& error) {
        require(error.code() == "hpo_walkforward_unavailable", "wrong typed failure code");
        require(error.origin() == pfh::FailureOrigin::Hpo, "wrong typed failure origin");
        require(error.args().size() == 1, "unexpected typed arguments");
        require(std::get<std::string>(error.args().at("reason").value()) == reason,
                std::string("wrong failure reason, wanted ") + reason + ": " + error.what());
        require(diagnostic == nullptr || std::string(error.what()).find(diagnostic) !=
                                             std::string::npos,
                "failure came from the wrong loader stage: " + std::string(error.what()));
        return error.what();
    }
    throw std::runtime_error(std::string("missing refusal: ") + reason);
}

pfh::PlanRequest request() {
    return {1704078000000LL, 1704088800000LL, 1704067200000LL, 2,
            "1", "60", "UTC", "UTC", "", true};
}

std::vector<pf_bar_t> bars(std::int64_t first = 1704067200000LL, std::size_t count = 360) {
    std::vector<pf_bar_t> result;
    result.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        const auto value = static_cast<double>(index);
        result.push_back({100 + value, 102 + value, 98 + value, 101 + value, 110 + value,
                          first + static_cast<std::int64_t>(index) * 60000});
    }
    return result;
}

pf_selected_plan_result_v1 golden() {
    // Independent literals from engine tests/test_selected_window_plan_c.cpp:385-427.
    pf_selected_plan_result_v1 result{};
    result.struct_size = 248;
    result.version = 1;
    result.status = 0;
    result.bound = -1;
    result.supplied_input_bars = 360;
    result.supplied_script_bars = 6;
    result.available_script_bars = 3;
    result.used_script_bars = 2;
    result.trimmed_script_bars = 1;
    result.trim_index = 60;
    result.fed_input_bars = 300;
    result.fed_script_bars = 5;
    result.preroll_input_bars = 120;
    result.window_input_bars = 180;
    result.window_script_bars = 3;
    result.trim_start_ms = 1704070800000LL;
    result.preroll_first_bar_ms = 1704070800000LL;
    result.preroll_last_bar_ms = 1704074400000LL;
    result.supplied_first_data_ms = 1704067200000LL;
    result.supplied_last_data_ms = 1704088740000LL;
    result.fed_first_data_ms = 1704070800000LL;
    result.fed_last_data_ms = 1704088740000LL;
    result.window_first_data_ms = 1704078000000LL;
    result.window_last_data_ms = 1704088740000LL;
    result.present_mask = 255;
    return result;
}

void expect_exact(const pfh::PlanResult& actual, const pf_selected_plan_result_v1& expected) {
    // Every byte is a declared field in this pinned 248-byte layout, including the option.
    require(std::memcmp(&actual.raw, &expected, sizeof(expected)) == 0,
            "raw planner result differs from hand-written expectation");
    require(actual.ok() == (expected.status == 0), "refusal was confused with successful planning");
    require(static_cast<std::int32_t>(actual.status()) == expected.status, "typed status differs");
}

pf_selected_plan_result_v1 refusal(std::int32_t status, const char* option,
                                  std::int32_t bound = -1, std::int64_t value = 0,
                                  std::int64_t previous = 0, std::int64_t next = 0) {
    pf_selected_plan_result_v1 result{};
    result.struct_size = 248;
    result.version = 1;
    result.status = status;
    result.bound = bound;
    result.value_ms = value;
    result.previous_boundary_ms = previous;
    result.next_boundary_ms = next;
    require(std::strlen(option) < sizeof(result.option), "bad test option");
    std::memcpy(result.option, option, std::strlen(option));
    return result;
}

void expect_origin(const pfh::WindowPlanHelper& helper) {
    Dl_info information{};
    require(::dladdr(reinterpret_cast<void*>(Access::raw_bridge(helper)), &information) != 0 &&
                information.dli_fname != nullptr,
            "cannot identify loaded planner symbol");
    const auto loaded = checked_path(information.dli_fname, "loaded symbol origin");
    require(fs::equivalent(loaded, helper.snapshot_path()),
            "planner symbol came from another file");
}

struct Probe {
    const pf_bar_t* seen = nullptr;
    std::uint64_t count = 0;
    pf_selected_plan_request_v1 request{};
    pf_selected_plan_result_v1 result{};
    int bridge_return = 0;
} probe;

int unit_bridge(const pf_selected_plan_request_v1* descriptor, const pf_bar_t* rows,
                std::uint64_t count, pf_selected_plan_result_v1* out) {
    require(descriptor != nullptr && out != nullptr, "wrapper passed a null descriptor");
    require(out->struct_size == 248 && out->version == 1, "wrapper did not preset output header");
    probe.seen = rows;
    probe.count = count;
    probe.request = *descriptor;
    *out = probe.result;
    return probe.bridge_return;
}

void unit_rows() {
    row("unit-no-copy-and-request-descriptors", [] {
        // Unit seam only: no library, version compatibility or self-test claim.
        auto helper = Access::unit_bridge(unit_bridge);
        const auto input = bars();
        const auto before = input;
        const auto wanted = request();
        probe = {};
        probe.result = golden();
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), wanted), golden());
        require(probe.seen == input.data() && probe.count == 360,
                "wrapper copied or repacked rows");
        require(std::memcmp(input.data(), before.data(), input.size() * sizeof(pf_bar_t)) == 0,
                "wrapper changed caller rows");
        const auto& raw = probe.request;
        require(raw.struct_size == 80 && raw.version == 1 &&
                    raw.start_ms == 1704078000000LL && raw.end_ms == 1704088800000LL &&
                    raw.fed_start_ms == 1704067200000LL && raw.preroll_bars == 2 &&
                    raw.feed_tolerant == 1,
                "wrapper changed request scalars");
        require(raw.input_tf == wanted.input_tf.c_str() &&
                    raw.script_tf == wanted.script_tf.c_str() &&
                    raw.chart_timezone == wanted.chart_timezone.c_str() &&
                    raw.engine_timezone == wanted.engine_timezone.c_str() &&
                    raw.session == wanted.session.c_str(),
                "wrapper did not borrow the exact request strings");
        (void)helper.plan_selected_primary(input.data() + 60, 300, wanted);
        require(probe.seen == input.data() + 60 && probe.count == 300, "offset pointer changed");
        (void)helper.plan_selected_primary(nullptr, 0, wanted);
        require(probe.seen == nullptr && probe.count == 0, "empty pointer changed");
    });
    row("unit-result-descriptor-invariants", [] {
        auto helper = Access::unit_bridge(unit_bridge);
        const auto input = bars();
        const auto call = [&] {
            (void)helper.plan_selected_primary(input.data(), input.size(), request());
        };
        probe = {};
        for (const unsigned member : {0u, 1u, 2u, 3u, 4u, 5u, 6u, 7u, 8u, 9u, 10u}) {
            probe.result = golden();
            switch (member) {
            case 0: probe.result.struct_size = 247; break;
            case 1: probe.result.version = 2; break;
            case 2: probe.result.reserved = 1; break;
            case 3: probe.result.status = 7; break;
            case 4: probe.result.bound = 3; break;
            case 5: probe.result.present_mask = 256; break;
            case 6: probe.result.shortfall = 2; break;
            case 7: probe.result.complete_pending_preroll_at_horizon = 2; break;
            case 8: std::memset(probe.result.option, 'x', sizeof(probe.result.option)); break;
            case 9: probe.result.option[1] = 'x'; break;
            case 10: probe.result.present_mask = 254; break;
            }
            expect_reason("window_plan_load", call);
        }
        // Negative C returns remain errors even when the output body looks like a valid plan.
        for (const int result : {-1, -2, -3}) {
            probe.result = golden();
            probe.bridge_return = result;
            expect_reason("window_plan_load", call);
        }
        probe.bridge_return = 0;
        for (std::int32_t status = 1; status <= 6; ++status) {
            probe.result = refusal(status, "");
            expect_exact(helper.plan_selected_primary(input.data(), input.size(), request()),
                         probe.result);
        }
    });
}

void loader_failure_rows(const fs::path& fake, const fs::path& base) {
    const auto fake_bytes = read_bytes(fake);
    row("relative-path-before-filesystem-access", [&] {
        expect_reason("window_plan_path", [&] {
            (void)pfh::WindowPlanHelper::load("relative-helper.so", std::string(64, '0'),
                                             "relative-directory");
        });
    });
    row("hash-mismatch-before-dlopen", [&] {
        const auto directory = private_directory(base, "mismatch");
        const auto source = directory / "not-a-library";
        const std::string bytes(fake_bytes.size(), 'x');  // Same size as a built library.
        write_bytes(source, bytes);
        auto wrong_digest = pfh::detail::sha256(bytes);
        wrong_digest[0] = wrong_digest[0] == '0' ? '1' : '0';
        expect_reason("window_plan_hash_mismatch", [&] {
            (void)pfh::WindowPlanHelper::load(source, wrong_digest, directory);
        }, "planner snapshot SHA-256 differs from expected");
    });
    row("verified-non-library-and-streaming-hash-padding", [&] {
        const auto directory = private_directory(base, "data");
        // Fixed SHA-256 vectors anchor the streaming implementation independently.
        const std::array<std::pair<std::string, std::string>, 2> vectors{{
            {"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
            {"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}}};
        std::size_t index = 0;
        for (const auto& vector : vectors) {
            const auto source = directory / ("known-" + std::to_string(index++));
            write_bytes(source, vector.first);
            expect_reason("window_plan_load", [&] {
                (void)pfh::WindowPlanHelper::load(source, vector.second, directory);
            }, "cannot load verified planner snapshot");
        }
        for (const std::size_t size : {55u, 56u, 63u, 64u, 65u, 65537u}) {
            const auto source = directory / ("padding-" + std::to_string(size));
            const std::string bytes(size, 'a');
            write_bytes(source, bytes);
            expect_reason("window_plan_load", [&] {
                (void)pfh::WindowPlanHelper::load(source, pfh::detail::sha256(bytes), directory);
            }, "cannot load verified planner snapshot");
        }
    });
    row("missing-helper-and-private-directory", [&] {
        const auto directory = private_directory(base, "missing");
        const auto absent = checked_path(directory / "absent", "missing helper");
        expect_reason("window_plan_load", [&] {
            (void)pfh::WindowPlanHelper::load(absent, std::string(64, '0'), directory);
        }, "cannot open planner source");
        const auto shared = private_directory(base, "shared");
        require(::chmod(shared.c_str(), 0755) == 0, "cannot prepare non-private directory");
        expect_reason("window_plan_load", [&] {
            (void)pfh::WindowPlanHelper::load(fake, pfh::detail::sha256(fake_bytes), shared);
        }, "planner snapshot directory must be private and user-owned");
    });
    row("non-sticky-group-writable-ancestor-refused", [&] {
        // Red if require_safe_ancestry is removed or accepts a non-sticky group-writable parent.
        const auto parent = checked_path(
            private_directory(base, "ancestry-parent"), "ancestry parent chmod");
        const auto directory = private_directory(parent, "private-child");
        require(::chmod(parent.c_str(), 0770) == 0, "cannot prepare unsafe ancestor");
        const auto diagnostic = expect_reason("window_plan_load", [&] {
            (void)pfh::WindowPlanHelper::load(fake, pfh::detail::sha256(fake_bytes), directory);
        });
        struct stat parent_identity {};
        require(::stat(parent.c_str(), &parent_identity) == 0,
                "cannot inspect unsafe ancestor");
        require(diagnostic.find(parent.string()) != std::string::npos,
                "ancestry diagnostic omitted the offending path");
        require(diagnostic.find(
                    "mode=" +
                    std::to_string(static_cast<unsigned long>(parent_identity.st_mode & 07777))) !=
                    std::string::npos,
                "ancestry diagnostic omitted the offending mode");
        require(diagnostic.find(
                    "uid=" + std::to_string(static_cast<unsigned long>(parent_identity.st_uid))) !=
                    std::string::npos,
                "ancestry diagnostic omitted the offending uid");
        require(::chmod(parent.c_str(), 0700) == 0, "cannot restore test ancestor permissions");
    });
    row("fifo-source-refused-without-writer", [&] {
        // Red (blocks) if O_NONBLOCK is removed; removing S_ISREG loses this refusal diagnostic.
        const auto directory = private_directory(base, "fifo");
        const auto source = checked_path(directory / "source.fifo", "FIFO create");
        require(::mkfifo(source.c_str(), 0600) == 0, "cannot create FIFO source");
        expect_reason("window_plan_load", [&] {
            (void)pfh::WindowPlanHelper::load(source, std::string(64, '0'), directory);
        }, "planner source must be a regular file");
    });
    row("fake-library-missing-version-export", [&] {
        const auto directory = private_directory(base, "missing-version");
        expect_reason("window_plan_missing_pf_selected_plan_version", [&] {
            (void)pfh::WindowPlanHelper::load(fake, pfh::detail::sha256(fake_bytes), directory);
        });
    });
    row("fake-library-missing-plan-export-binding-unit", [&] {
        // The default fake lacks BOTH exports. Check the actual production second binder
        // independently, since load() correctly stops at the first absent export.
        const auto checked = checked_path(fake, "fake symbol binding read");
        void* library = ::dlopen(checked.c_str(), RTLD_NOW | RTLD_LOCAL);
        require(library != nullptr, "cannot load built default fake library");
        try {
            expect_reason("window_plan_missing_pf_plan_selected_primary_v1", [&] {
                Access::require_plan_export(library);
            });
        } catch (...) {
            ::dlclose(library);
            throw;
        }
        require(::dlclose(library) == 0, "cannot close default fake library");
    });
}

void real_vectors(const pfh::WindowPlanHelper& helper) {
    const auto input = bars();
    row("real-golden-and-snapshot-symbol-origin", [&] {
        require(helper.version() == 1, "real helper version differs");
        require(std::string(helper.self_test_id()) == "selected-plan-utc-six-hours/v1",
                "self-test ID differs");
        require(helper.snapshot_path().is_absolute() &&
                    helper.snapshot_path() != helper.source_path(),
                "helper did not use an absolute separate snapshot");
        struct stat source{};
        require(::stat(helper.source_path().c_str(), &source) == 0 &&
                    helper.source_device() == static_cast<std::uint64_t>(source.st_dev) &&
                    helper.source_inode() == static_cast<std::uint64_t>(source.st_ino) &&
                    helper.source_size() == static_cast<std::uint64_t>(source.st_size),
                "helper did not retain the opened source identity");
        struct stat identity{};
        require(::stat(helper.snapshot_path().c_str(), &identity) == 0 &&
                    (identity.st_mode & 0777) == 0400,
                "successful snapshot is not read-only");
        require(pfh::detail::sha256(read_bytes(helper.snapshot_path())) == helper.sha256(),
                "loaded snapshot bytes differ");
        expect_origin(helper);
        const auto before = input;
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), request()), golden());
        require(std::memcmp(input.data(), before.data(), input.size() * sizeof(pf_bar_t)) == 0,
                "real planner changed immutable rows");
    });
    row("real-boundary-T-E-F-plus-one-minute", [&] {
        const std::array<pf_selected_plan_result_v1, 3> expected{{
            refusal(3, "start_ms", 0, 1704078060000LL, 1704078000000LL, 1704081600000LL),
            refusal(3, "end_ms", 1, 1704088860000LL, 1704088800000LL, 1704092400000LL),
            refusal(3, "fed_start_ms", 2, 1704067260000LL, 1704067200000LL, 1704070800000LL)}};
        for (unsigned boundary = 0; boundary < 3; ++boundary) {
            auto changed = request();
            if (boundary == 0) changed.start_ms += 60000;
            if (boundary == 1) changed.end_ms += 60000;
            if (boundary == 2) changed.fed_start_ms += 60000;
            expect_exact(helper.plan_selected_primary(input.data(), input.size(), changed),
                         expected[boundary]);
        }
    });
    row("real-zero-preroll-and-minute-passthrough", [&] {
        auto wanted = request();
        wanted.preroll_bars = 0;
        auto expected = golden();
        expected.used_script_bars = 0;
        expected.trimmed_script_bars = 3;
        expected.trim_index = 180;
        expected.fed_input_bars = 180;
        expected.fed_script_bars = 3;
        expected.preroll_input_bars = 0;
        expected.trim_start_ms = 1704078000000LL;
        expected.preroll_first_bar_ms = 0;
        expected.preroll_last_bar_ms = 0;
        expected.fed_first_data_ms = 1704078000000LL;
        expected.present_mask = 252;
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), wanted), expected);
        wanted = request();
        wanted.script_tf = "1";
        expected = golden();
        expected.supplied_script_bars = 360;
        expected.available_script_bars = 180;
        expected.trimmed_script_bars = 178;
        expected.trim_index = 178;
        expected.fed_input_bars = 182;
        expected.fed_script_bars = 182;
        expected.preroll_input_bars = 2;
        expected.window_script_bars = 180;
        expected.trim_start_ms = 1704077880000LL;
        expected.preroll_first_bar_ms = 1704077880000LL;
        expected.preroll_last_bar_ms = 1704077940000LL;
        expected.fed_first_data_ms = 1704077880000LL;
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), wanted), expected);
    });
    row("real-direct-array-duplicate-and-descending-rows", [&] {
        auto unordered = bars(1704070800000LL, 2);
        unordered[1].timestamp = 1704070800000LL;
        expect_exact(helper.plan_selected_primary(unordered.data(), unordered.size(), request()),
                     refusal(5, "rows", -1, 1704070800000LL, 1704070800000LL));
        unordered[1].timestamp = 1704070740000LL;
        expect_exact(helper.plan_selected_primary(unordered.data(), unordered.size(), request()),
                     refusal(5, "rows", -1, 1704070740000LL, 1704070800000LL));
    });
    row("real-feed-below-F-and-at-E", [&] {
        const auto below = bars(1704067140000LL, 2);
        expect_exact(helper.plan_selected_primary(below.data(), below.size(), request()),
                     refusal(4, "rows", 2, 1704067140000LL, 1704067200000LL, 1704088800000LL));
        const auto at_end = bars(1704088800000LL, 1);
        expect_exact(helper.plan_selected_primary(at_end.data(), at_end.size(), request()),
                     refusal(4, "rows", 1, 1704088800000LL, 1704067200000LL, 1704088800000LL));
    });
    row("real-clock-and-timeframe-pairing-refusals", [&] {
        auto changed = request();
        changed.engine_timezone = "America/New_York";
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), changed),
                     refusal(2, "timezone"));
        changed = request();
        changed.input_tf = "60";
        changed.script_tf = "15";
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), changed),
                     refusal(2, "timeframes"));
    });
    row("real-request-refusals-and-unsigned-negative-N", [&] {
        auto changed = request();
        // N is uint32_t in BOTH engine request declarations. The C representation of -1 is
        // 4294967295, which is refused as a planner request, not as a bridge header failure.
        changed.preroll_bars = static_cast<std::uint32_t>(-1);
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), changed),
                     refusal(1, "preroll_bars", -1, 4294967295LL));
        changed = request();
        changed.start_ms = changed.end_ms;
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), changed),
                     refusal(1, "end_ms", 1, 1704088800000LL, 1704088800000LL));
        changed.start_ms += 60000;
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), changed),
                     refusal(1, "end_ms", 1, 1704088800000LL, 1704088860000LL));
        expect_exact(helper.plan_selected_primary(nullptr, 3, request()), refusal(1, "rows"));
    });
    row("real-bridge-bad-request-size-is-negative-and-output-unchanged", [&] {
        // Raw bridge-only descriptor row: the typed wrapper always constructs a valid header.
        const auto wanted = request();
        pf_selected_plan_request_v1 descriptor{};
        descriptor.struct_size = 79;
        descriptor.version = 1;
        descriptor.start_ms = wanted.start_ms;
        descriptor.end_ms = wanted.end_ms;
        descriptor.fed_start_ms = wanted.fed_start_ms;
        descriptor.preroll_bars = 2;
        descriptor.feed_tolerant = 1;
        descriptor.input_tf = "1";
        descriptor.script_tf = "60";
        descriptor.chart_timezone = "UTC";
        descriptor.engine_timezone = "UTC";
        descriptor.session = "";
        auto result = golden();
        const auto before = result;
        require(Access::raw_bridge(helper)(&descriptor, input.data(), 360, &result) == -1,
                "bad request header was not a bridge failure");
        require(std::memcmp(&result, &before, sizeof(result)) == 0,
                "negative bridge return changed output");
    });
    row("real-shortfall-with-pending-preroll-completion", [&] {
        const auto short_input = bars(1704070800000LL, 90);
        auto wanted = request();
        wanted.preroll_bars = 3;
        pf_selected_plan_result_v1 expected{};
        expected.struct_size = 248;
        expected.version = 1;
        expected.bound = -1;
        expected.supplied_input_bars = 90;
        expected.supplied_script_bars = 2;
        expected.available_script_bars = 2;
        expected.used_script_bars = 2;
        expected.fed_input_bars = 90;
        expected.fed_script_bars = 2;
        expected.preroll_input_bars = 90;
        expected.trim_start_ms = 1704070800000LL;
        expected.preroll_first_bar_ms = 1704070800000LL;
        expected.preroll_last_bar_ms = 1704074400000LL;
        expected.supplied_first_data_ms = 1704070800000LL;
        expected.supplied_last_data_ms = 1704076140000LL;
        expected.fed_first_data_ms = 1704070800000LL;
        expected.fed_last_data_ms = 1704076140000LL;
        expected.present_mask = 63;
        expected.shortfall = 1;
        expected.complete_pending_preroll_at_horizon = 1;
        expect_exact(helper.plan_selected_primary(short_input.data(), short_input.size(), wanted),
                     expected);
    });
}

void swap_rows(const std::string& original, const std::string& digest, const fs::path& base) {
    row("real-source-path-replacement-keeps-original-or-detects-ctime", [&] {
        // Linux filesystems commonly update the held inode's ctime on rename, so same_source
        // normally refuses this swap. Both outcomes are valid; the symlink row below proves
        // that copying uses the opened descriptor even when the source pathname is replaced.
        const auto directory = private_directory(base, "swap");
        const auto source = directory / "source";
        const auto replacement = directory / "replacement";
        const auto saved = checked_path(directory / "original-retained", "rename destination");
        write_bytes(source, original);
        write_bytes(replacement, std::string(original.size(), 'x'));
        unsigned hooks = 0;
        try {
            auto helper = Access::synchronized_load(source, digest, directory, [&] {
                ++hooks;
                fs::rename(source, saved);
                fs::rename(replacement, source);
            });
            require(hooks == 1, "source swap barrier did not run exactly once");
            require(read_bytes(helper.snapshot_path()) == original,
                    "replacement bytes were loaded");
            expect_origin(helper);
            const auto input = bars();
            expect_exact(helper.plan_selected_primary(input.data(), input.size(), request()),
                         golden());
        } catch (const pfh::TypedHpoError<std::runtime_error>& error) {
            require(hooks == 1 && error.code() == "hpo_walkforward_unavailable" &&
                        std::get<std::string>(error.args().at("reason").value()) ==
                            "window_plan_changed",
                    "source swap neither kept original bytes nor detected source mutation");
        }
    });
    row("real-symlink-swap-keeps-opened-original", [&] {
        // Red if snapshotting reopens the replaced pathname instead of reading the held fd.
        const auto directory = private_directory(base, "symlink");
        const auto target = directory / "original";
        const auto other = directory / "replacement";
        const auto link = checked_path(directory / "helper-link", "symlink write");
        const auto replacement_link = checked_path(directory / "replacement-link", "symlink write");
        const auto saved_link = checked_path(directory / "saved-link", "rename destination");
        write_bytes(target, original);
        write_bytes(other, std::string(original.size(), 'x'));
        struct stat original_identity{};
        require(::stat(target.c_str(), &original_identity) == 0, "cannot inspect symlink target");
        fs::create_symlink(target, link);
        fs::create_symlink(other, replacement_link);
        unsigned hooks = 0;
        auto helper = Access::synchronized_load(link, digest, directory, [&] {
            ++hooks;
            fs::rename(link, saved_link);
            fs::rename(replacement_link, link);
        });
        require(hooks == 1, "symlink barrier did not run exactly once");
        require(helper.source_device() == static_cast<std::uint64_t>(original_identity.st_dev) &&
                    helper.source_inode() == static_cast<std::uint64_t>(original_identity.st_ino),
                "source identity was taken from the replacement symlink target");
        require(read_bytes(helper.snapshot_path()) == original,
                "swapped symlink changed loaded bytes");
        expect_origin(helper);
        const auto input = bars();
        expect_exact(helper.plan_selected_primary(input.data(), input.size(), request()), golden());
    });
    row("real-same-inode-mutation-after-open-barrier", [&] {
        // Red if the final same_source check is removed: appended bytes are outside the copy.
        const auto directory = private_directory(base, "in-place");
        const auto source = directory / "source";
        write_bytes(source, original);
        struct stat before{};
        require(::stat(source.c_str(), &before) == 0, "cannot inspect source before mutation");
        unsigned hooks = 0;
        expect_reason("window_plan_changed", [&] {
            (void)Access::synchronized_load(source, digest, directory, [&] {
                ++hooks;
                const auto named = checked_path(source, "same-inode append");
                const int descriptor = ::open(named.c_str(), O_WRONLY | O_APPEND | O_CLOEXEC);
                require(descriptor >= 0, "cannot open source for in-place mutation");
                const auto written = ::write(descriptor, "x", 1);
                const int closed = ::close(descriptor);
                require(written == 1 && closed == 0, "cannot mutate source in place");
                struct stat after{};
                require(::stat(source.c_str(), &after) == 0 &&
                            before.st_dev == after.st_dev && before.st_ino == after.st_ino &&
                            after.st_size == before.st_size + 1,
                        "in-place fixture replaced the inode instead of mutating it");
            });
        });
        require(hooks == 1, "in-place barrier did not run exactly once");
    });
    row("real-source-shrink-after-open-barrier", [&] {
        // Red/nonterminating if the received == 0 refusal is removed: copied stays below size.
        const auto directory = private_directory(base, "shrink");
        const auto source = directory / "source";
        write_bytes(source, original);
        struct stat before{};
        require(::stat(source.c_str(), &before) == 0 && before.st_size > 0,
                "cannot inspect nonempty source before shrinking");
        unsigned hooks = 0;
        expect_reason("window_plan_changed", [&] {
            (void)Access::synchronized_load(source, digest, directory, [&] {
                ++hooks;
                const auto named = checked_path(source, "same-inode truncate");
                const int descriptor = ::open(named.c_str(), O_WRONLY | O_CLOEXEC);
                require(descriptor >= 0, "cannot open source for shrinking");
                const int truncated = ::ftruncate(descriptor, before.st_size - 1);
                struct stat after{};
                const int inspected = ::fstat(descriptor, &after);
                const int closed = ::close(descriptor);
                require(truncated == 0 && inspected == 0 && closed == 0,
                        "cannot shrink source through second descriptor");
                require(before.st_dev == after.st_dev && before.st_ino == after.st_ino &&
                            after.st_size == before.st_size - 1,
                        "shrink fixture did not truncate the held source inode");
            });
        }, "planner source shrank while snapshotting");
        require(hooks == 1, "shrink barrier did not run exactly once");
    });
    row("real-snapshot-replaced-after-close", [&] {
        // Red if !same_source(snapshot_identity, snapshot_now) is removed: the replacement is
        // another regular 0400 file containing a valid helper, so dlopen would succeed.
        const auto directory = private_directory(base, "snapshot-replaced");
        const auto source = directory / "source";
        const auto replacement = checked_path(directory / "replacement", "replacement chmod");
        const auto saved = checked_path(directory / "saved-snapshot", "rename destination");
        write_bytes(source, original);
        write_bytes(replacement, original);
        require(::chmod(replacement.c_str(), 0400) == 0, "cannot make replacement read-only");
        unsigned hooks = 0;
        expect_reason("window_plan_changed", [&] {
            (void)Access::synchronized_load(
                source, digest, directory, {}, [&](const fs::path& path) {
                    ++hooks;
                    const auto snapshot = checked_path(path, "completed snapshot rename source");
                    fs::rename(snapshot, saved);
                    fs::rename(replacement, snapshot);
                });
        }, "planner snapshot path changed before loading");
        require(hooks == 1, "snapshot replacement barrier did not run exactly once");
    });
    row("real-snapshot-writable-after-close", [&] {
        // Red if the post-close snapshot identity AND write-bit rejection terms are removed:
        // chmod may change ctime too, and the unchanged helper bytes remain loadable.
        const auto directory = private_directory(base, "snapshot-writable");
        const auto source = directory / "source";
        write_bytes(source, original);
        unsigned hooks = 0;
        expect_reason("window_plan_changed", [&] {
            (void)Access::synchronized_load(
                source, digest, directory, {}, [&](const fs::path& path) {
                    ++hooks;
                    const auto snapshot = checked_path(path, "completed snapshot chmod");
                    require(::chmod(snapshot.c_str(), 0600) == 0, "cannot make snapshot writable");
                });
        }, "planner snapshot path changed before loading");
        require(hooks == 1, "snapshot chmod barrier did not run exactly once");
    });
    row("real-snapshot-directory-replaced-after-close", [&] {
        // Red if the whole post-close directory/file identity refusal block is removed:
        // retained directory: "snapshot-directory-retained"; decoy: "snapshot-directory".
        // The decoy is private (0700) and its same-named 0400 file is a loadable helper.
        const auto directory = checked_path(
            private_directory(base, "snapshot-directory"), "snapshot directory rename source");
        const auto saved = checked_path(
            directory.parent_path() / (directory.filename().string() + "-retained"),
            "directory rename destination");
        const auto source = directory / "source";
        write_bytes(source, original);
        unsigned hooks = 0;
        expect_reason("window_plan_changed", [&] {
            (void)Access::synchronized_load(
                source, digest, directory, {}, [&](const fs::path& path) {
                    ++hooks;
                    const auto snapshot = checked_path(path, "snapshot in directory to replace");
                    fs::rename(directory, saved);
                    require(::mkdir(directory.c_str(), 0700) == 0, "cannot create decoy directory");
                    require(::chmod(directory.c_str(), 0700) == 0, "cannot make decoy private");
                    write_bytes(snapshot, original);
                    require(::chmod(snapshot.c_str(), 0400) == 0,
                            "cannot make decoy file read-only");
                });
        }, "planner snapshot path changed before loading");
        require(hooks == 1, "directory replacement barrier did not run exactly once");
    });
    row("real-snapshot-lifetime-and-move-ownership", [&] {
        const auto directory = private_directory(base, "lifetime");
        const auto source = directory / "source";
        const auto saved = checked_path(directory / "saved-original", "rename destination");
        write_bytes(source, original);
        auto first = pfh::WindowPlanHelper::load(source, digest, directory);
        const auto snapshot = first.snapshot_path();
        fs::rename(source, saved);
        write_bytes(source, std::string(original.size(), 'x'));
        auto moved = std::move(first);
        const auto input = bars();
        expect_exact(moved.plan_selected_primary(input.data(), input.size(), request()), golden());
        expect_origin(moved);
        require(fs::is_regular_file(snapshot), "snapshot disappeared while the worker owns it");
        expect_reason("window_plan_load", [&] {
            (void)first.plan_selected_primary(input.data(), input.size(), request());
        });
    });
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 3, "usage: test_window_plan FAKE_STRATEGY ASSIGNED_RUN_DIRECTORY");
        const auto fake = checked_path(argv[1], "built fake library");
        const auto base = checked_path(argv[2], "assigned run root write");
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
        loader_failure_rows(fake, base);
        scenario = "real-helper-availability";
        if (helper_path.empty() || !fs::is_regular_file(helper_path)) {
            std::cout << "ROW real-helper UNAVAILABLE: PFH_WINDOW_PLAN_LIB is unset or missing\n";
            require(!required, "PFH_REQUIRE_WINDOW_PLAN=1 requires the built real helper");
            std::cout << "RUN.COMPLETE window-plan UNAVAILABLE\n";
            return 0;
        }
        const auto bytes = read_bytes(helper_path);
        const auto digest = pfh::detail::sha256(bytes);
        const auto directory = private_directory(base, "real-helper");
        auto helper = pfh::WindowPlanHelper::load(helper_path, digest, directory);
        real_vectors(helper);
        swap_rows(bytes, digest, base);
        std::cout << "RUN.COMPLETE window-plan PASS real-helper=present\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ROW " << scenario << " FAIL: " << error.what() << '\n';
        return 1;
    }
}
