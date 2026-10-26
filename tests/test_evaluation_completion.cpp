// A separate translation of the real CLI, like test_signal_macro.cpp. Only this translation
// defines the hook macro; the production and proof-curves targets retain their original code.
#include <atomic>
#include <chrono>

namespace evaluation_completion_test {
struct Clock {
    using time_point = std::chrono::steady_clock::time_point;
    static time_point now() noexcept;
};
void evaluation_complete();
void output_drain();
}  // namespace evaluation_completion_test

#define PINEFORGE_HPO_TEST_EVALUATION_COMPLETION 1
#define main pineforge_hpo_native_main
#include "../src/cli/main.cpp"
#undef main

namespace evaluation_completion_test {
namespace {
std::atomic<bool> controlled{false};
std::atomic<bool> deadline_reached{false};
int command_fd = -1;
int event_fd = -1;
std::mutex gate_mutex;
std::condition_variable gate_changed;
bool evaluation_released = false;
bool drain_released = false;
bool drain_seen = false;

[[noreturn]] void protocol_error(const std::string& message) {
    std::cerr << "evaluation-completion test protocol: " << message << '\n';
    ::_exit(90);
}

void write_all(int descriptor, const std::string& value) {
    std::size_t offset = 0;
    while (offset < value.size()) {
        const auto count = ::write(descriptor, value.data() + offset, value.size() - offset);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            protocol_error("cannot write receipt");
        offset += static_cast<std::size_t>(count);
    }
}

void gate(char event, bool& released) {
    if (!controlled.load())
        return;
    std::unique_lock<std::mutex> lock(gate_mutex);
    write_all(event_fd, std::string(1, event));
    gate_changed.wait(lock, [&] { return released; });
}

void serve_commands() {
    for (;;) {
        char command = '\0';
        ssize_t count;
        do {
            count = ::read(command_fd, &command, 1);
        } while (count < 0 && errno == EINTR);
        if (count != 1)
            protocol_error("command channel closed");
        if (command == 'Q')
            return;
        if (command == 'S') {
            // The parent sends the real SIGTERM. Acknowledge handler delivery, not RunState's
            // observation: this must never call stopped() or set the study's stop reason.
            while (!pineforge::hpo::cli::signal_cancelled.load(std::memory_order_relaxed))
                std::this_thread::yield();
            write_all(event_fd, "S");
        } else if (command == 'D') {
            // Exactly equal to --max-wall-seconds 1, with no real-time admission budget.
            deadline_reached.store(true);
            write_all(event_fd, "D");
        } else if (command == 'E' || command == 'L') {
            {
                std::lock_guard<std::mutex> lock(gate_mutex);
                (command == 'E' ? evaluation_released : drain_released) = true;
            }
            gate_changed.notify_all();
        } else {
            protocol_error("unknown command");
        }
    }
}

bool forbidden_path(const std::filesystem::path& path) {
    const auto text = path.generic_string();
    for (const auto* name : {"/.ssh", "/.aws", "/.codex", "/.config/zsh/keys",
                             "/.config/pineforge"}) {
        const std::string suffix = name;
        if (text.find(suffix + "/") != std::string::npos ||
            (text.size() >= suffix.size() &&
             text.compare(text.size() - suffix.size(), suffix.size(), suffix) == 0))
            return true;
    }
    const auto name = path.filename().string();
    return name == ".env" || path.extension() == ".env" ||
           name.find(".env.") != std::string::npos;
}

void list_followed_files(int argc, char** argv, int manifest_fd) {
    // This instrumented copy accepts only the small CLI surface used by its driver. Inspect
    // names first and refuse symlinks rather than following them into a forbidden directory.
    const std::map<std::string, int> arities = {
        {"--strategy", 1}, {"--ohlcv", 1}, {"--output", 1}, {"--trials-file", 1},
        {"--objective", 1}, {"--sampler", 1}, {"--seed", 1}, {"--max-trials", 1},
        {"--workers", 1}, {"--batch-size", 1}, {"--batch-lag", 1}, {"--schedule", 1},
        {"--int-dim", 4}, {"--input-tf", 1}, {"--script-tf", 1}, {"--bar-magnifier", 1},
        {"--magnifier-samples", 1}, {"--magnifier-distribution", 1}, {"--fixed-input", 2},
        {"--categorical-choice", 2}, {"--progress-fd", 1}, {"--no-improvement-trials", 1},
        {"--max-wall-seconds", 1},
    };
    const std::vector<std::string> file_options = {
        "--strategy", "--ohlcv", "--output", "--trials-file",
    };
    std::string manifest = "Files followed by the test CLI (inputs and outputs):\n";
    if (argc == 2 && (std::string(argv[1]) == "--help" ||
                      std::string(argv[1]) == "--test-crash")) {
        write_all(manifest_fd, manifest + "(none)\n");
        return;
    }
    if (argc < 2 || std::string(argv[1]) != "run")
        protocol_error("only run, --help and --test-crash are admitted");
    for (int index = 2; index < argc; ++index) {
        const std::string option = argv[index];
        const auto found = arities.find(option);
        if (found == arities.end() || argc - index - 1 < found->second)
            protocol_error("unlisted option or missing value");
        if (std::find(file_options.begin(), file_options.end(), option) != file_options.end()) {
            const std::filesystem::path named = std::filesystem::absolute(argv[index + 1]);
            const auto normalized = named.lexically_normal();
            if (forbidden_path(named) || forbidden_path(normalized))
                protocol_error("credential or environment path refused by name: " + named.string());
            std::filesystem::path prefix;
            for (const auto& component : normalized) {
                prefix /= component;
                std::error_code error;
                const auto status = std::filesystem::symlink_status(prefix, error);
                if (error && error != std::errc::no_such_file_or_directory)
                    protocol_error("cannot inspect file name: " + prefix.string());
                if (std::filesystem::is_symlink(status))
                    protocol_error("symlink refused without following it: " + prefix.string());
            }
            manifest += option + " " + normalized.string() + "\n";
        }
        index += found->second;
    }
    write_all(manifest_fd, manifest);
}
}  // namespace

Clock::time_point Clock::now() noexcept {
    if (!controlled.load())
        return std::chrono::steady_clock::now();
    return time_point{} + std::chrono::seconds(deadline_reached.load() ? 1 : 0);
}

void evaluation_complete() {
    gate('E', evaluation_released);
}

void output_drain() {
    // shutdown() is also called by the destructor. Only the explicit first drain is gated.
    if (drain_seen)
        return;
    drain_seen = true;
    gate('L', drain_released);
}
}  // namespace evaluation_completion_test

int main(int argc, char** argv) {
    using namespace evaluation_completion_test;
    try {
        if (argc < 6 || std::string(argv[1]) != "--test-channels")
            protocol_error("expected --test-channels COMMAND_FD EVENT_FD MANIFEST_FD");
        command_fd = std::stoi(argv[2]);
        event_fd = std::stoi(argv[3]);
        const int manifest_fd = std::stoi(argv[4]);
        std::vector<char*> native_arguments{argv[0]};
        native_arguments.insert(native_arguments.end(), argv + 5, argv + argc);
        native_arguments.push_back(nullptr);
        const auto native_argc = static_cast<int>(native_arguments.size() - 1);
        list_followed_files(native_argc, native_arguments.data(), manifest_fd);
        if (std::string(native_arguments[1]) == "--test-crash")
            std::abort();
        controlled.store(command_fd >= 0 && event_fd >= 0);
        std::thread controller;
        if (controlled.load())
            controller = std::thread(serve_commands);
        const auto result = pineforge_hpo_native_main(native_argc, native_arguments.data());
        if (controller.joinable())
            controller.join();
        return result;
    } catch (const std::exception& error) {
        std::cerr << "evaluation-completion test runner: " << error.what() << '\n';
        return 90;
    }
}
