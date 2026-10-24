#include "trial_spool.hpp"

#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/resource.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

using pineforge::hpo::TypedHpoError;
using pineforge::hpo::detail::TrialSpool;

static_assert(noexcept(std::declval<TrialSpool&>().close()), "spool close must be noexcept");

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

void refuse_protected_path(const std::filesystem::path& path) {
    std::vector<std::string> parts;
    for (const auto& part : path)
        parts.push_back(part.string());
    for (std::size_t index = 0; index < parts.size(); ++index) {
        const auto& name = parts[index];
        const bool env = name.size() >= 4 && name.compare(name.size() - 4, 4, ".env") == 0;
        const bool config = name == ".config" && index + 1 < parts.size() &&
            (parts[index + 1] == "pineforge" ||
             (parts[index + 1] == "zsh" && index + 2 < parts.size() &&
              parts[index + 2] == "keys"));
        require(name != ".ssh" && name != ".aws" && name != ".codex" && !env && !config,
                "refused protected path by name: " + path.string());
    }
}

std::filesystem::path spool_output(const std::filesystem::path& requested) {
    const auto absolute = std::filesystem::absolute(requested);
    refuse_protected_path(absolute);
    const auto directory = std::filesystem::weakly_canonical(absolute);
    refuse_protected_path(directory);
    const auto output = directory / "result.json";
    // The output path selects the spool's parent; the test never opens result.json.
    std::cout << "FILES trial spool test:\n  " << directory << "\n  " << output
              << "\n  " << directory / ".pineforge-hpo-trials-data-XXXXXX"
              << "\n  " << directory / ".pineforge-hpo-trials-index-XXXXXX" << std::endl;
    std::filesystem::create_directories(directory);
    require(::access(directory.c_str(), W_OK) == 0, "spool test directory must be writable");
    return output;
}

// Restore both process controls on every exception path as well as the successful path.
class FileSizeLimit final {
public:
    FileSizeLimit() {
        require(::getrlimit(RLIMIT_FSIZE, &saved_limit_) == 0, "cannot read RLIMIT_FSIZE");
        require(::sigaction(SIGXFSZ, nullptr, &saved_signal_) == 0, "cannot read SIGXFSZ");
    }

    FileSizeLimit(const FileSizeLimit&) = delete;
    FileSizeLimit& operator=(const FileSizeLimit&) = delete;

    ~FileSizeLimit() {
        if (!restore())
            std::abort();
    }

    void apply(rlim_t bytes = 4096) {
        require(saved_limit_.rlim_max == RLIM_INFINITY || saved_limit_.rlim_max >= bytes,
                "RLIMIT_FSIZE hard limit is below the requested control");
        struct sigaction ignored {};
        ignored.sa_handler = SIG_IGN;
        require(sigemptyset(&ignored.sa_mask) == 0, "cannot initialize SIGXFSZ mask");
        require(::sigaction(SIGXFSZ, &ignored, nullptr) == 0, "cannot ignore SIGXFSZ");
        signal_changed_ = true;
        auto limited = saved_limit_;
        limited.rlim_cur = bytes;
        require(::setrlimit(RLIMIT_FSIZE, &limited) == 0, "cannot set RLIMIT_FSIZE");
        limit_changed_ = true;
    }

    bool restore() noexcept {
        bool restored = true;
        if (limit_changed_) {
            if (::setrlimit(RLIMIT_FSIZE, &saved_limit_) == 0)
                limit_changed_ = false;
            else
                restored = false;
        }
        if (signal_changed_) {
            if (::sigaction(SIGXFSZ, &saved_signal_, nullptr) == 0)
                signal_changed_ = false;
            else
                restored = false;
        }
        return restored;
    }

    void require_restored() const {
        struct rlimit current {};
        struct sigaction action {};
        require(::getrlimit(RLIMIT_FSIZE, &current) == 0 &&
                    current.rlim_cur == saved_limit_.rlim_cur &&
                    current.rlim_max == saved_limit_.rlim_max,
                "RLIMIT_FSIZE was not restored");
        require(::sigaction(SIGXFSZ, nullptr, &action) == 0 &&
                    action.sa_handler == saved_signal_.sa_handler,
                "SIGXFSZ was not restored");
    }

private:
    struct rlimit saved_limit_ {};
    struct sigaction saved_signal_ {};
    bool limit_changed_ = false;
    bool signal_changed_ = false;
};

template <typename Action>
std::string output_refusal(Action action, const std::string& expected) {
    try {
        action();
    } catch (const TypedHpoError<std::runtime_error>& error) {
        require(error.code() == "hpo_output_io_failed", "wrong spool refusal code");
        const std::string message = error.what();
        require(message.find(expected) != std::string::npos,
                "wrong spool refusal message: " + message);
        return message;
    }
    throw std::runtime_error("spool operation did not throw a TypedHpoError");
}

void seal_rethrows_first_failure(const std::filesystem::path& output) {
    TrialSpool spool(output, 0);
    FileSizeLimit limit;
    const std::string line = "{\"payload\":\"" + std::string(1024, 'x') + "\"}\n";
    limit.apply(static_cast<rlim_t>(line.size() * 3));
    std::uint64_t completed = 0;
    const auto first = output_refusal([&] {
        for (; completed < 4; ++completed)
            spool.append(completed, line);
    }, "failed writing terminal trial spool");
    require(completed == 3, "exact file-size limit did not stop before the fourth row");
    const auto sealed = output_refusal([&] { spool.seal(completed); },
                                      "failed writing terminal trial spool");
    require(sealed == first, "seal did not rethrow the original spool failure");
    require(limit.restore(), "cannot restore file-size controls");
    limit.require_restored();
    spool.close();
    std::cout << "PASS seal rethrows the first failure before finalization checks\n";
}

void write_failure_keeps_first_message(const std::filesystem::path& output) {
    TrialSpool spool(output, 0);
    FileSizeLimit limit;
    limit.apply();
    std::uint64_t completed = 0;
    const std::string line = "{\"payload\":\"" + std::string(1024, 'x') + "\"}\n";
    const auto first = output_refusal([&] {
        for (; completed < 32; ++completed)
            spool.append(completed, line);
    }, "failed writing terminal trial spool");
    const auto repeated = output_refusal(
        [&] { spool.append(completed + 1, line); }, "failed writing terminal trial spool");
    require(repeated == first, "a later spool failure replaced the first failure");
    const auto sealed = output_refusal([&] { spool.seal(completed); },
                                      "failed writing terminal trial spool");
    require(sealed == first, "seal did not preserve the first spool failure after a retry");
    require(limit.restore(), "cannot restore file-size controls");
    limit.require_restored();
    spool.close();
    spool.close();
    std::cout << "PASS spool write failure is typed\n"
              << "PASS repeated spool failure keeps the first message\n"
              << "PASS seal rethrows the same first message after a retry\n"
              << "PASS RLIMIT_FSIZE and SIGXFSZ restored\n";
}

void close_is_idempotent(const std::filesystem::path& output) {
    TrialSpool closed(output, 0);
    closed.close();
    // Reuse the released descriptor slots. A second close must not close this live spool.
    TrialSpool live(output, 0);
    closed.close();
    live.append(0, "{}\n");
    live.seal(1);
    std::ostringstream text;
    live.copy_array(text);
    require(text.str() == "    {}\n", "second close damaged a later spool");
    live.close();
    live.close();
    std::cout << "PASS close is noexcept and idempotent\n";
}

void relative_slots_preserve_bytes(const std::filesystem::path& output,
                                  std::uint64_t first_trial_id) {
    TrialSpool spool(output, first_trial_id);
    spool.append(first_trial_id + 3, "{\"slot\":3}\n");
    spool.append(first_trial_id, "{\"slot\":0}\n");
    spool.append(first_trial_id + 2, "{\"slot\":2}\n");
    spool.seal(3);
    std::ostringstream text;
    spool.copy_array(text);
    const std::string expected = "    {\"slot\":0},\n    {\"slot\":2},\n    {\"slot\":3}\n";
    require(text.str() == expected, "relative/gapped trial array bytes differ");
    require(spool.array_bytes() == expected.size(), "trial array byte count differs");
    std::cout << "PASS relative slots and exact array bytes, first ID " << first_trial_id << '\n';
}

void invalid_ids_are_typed(const std::filesystem::path& output) {
    // Without the lower-bound guard, subtraction wraps to relative slot 1 and otherwise succeeds.
    TrialSpool below_first(output, std::numeric_limits<std::uint64_t>::max());
    output_refusal([&] { below_first.append(0, "{}\n"); }, "outside the index range");
    std::cout << "PASS ID below first_trial_id is a typed refusal\n";
    TrialSpool duplicate(output, 0);
    duplicate.append(2, "{}\n");
    output_refusal([&] { duplicate.append(2, "{}\n"); }, "duplicate terminal trial spool ID");
    std::cout << "PASS duplicate ID is a typed refusal\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: test_trial_spool BUILD_SCRATCH_DIRECTORY");
        const auto output = spool_output(argv[1]);
        seal_rethrows_first_failure(output);
        write_failure_keeps_first_message(output);
        close_is_idempotent(output);
        relative_slots_preserve_bytes(output, 0);
        relative_slots_preserve_bytes(output, std::numeric_limits<std::uint64_t>::max() - 3);
        invalid_ids_are_typed(output);
        std::cout << "PASS trial spool tests\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL trial spool tests: " << error.what() << '\n';
        return 1;
    }
}
