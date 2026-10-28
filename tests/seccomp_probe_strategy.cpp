// Real syscall probes loaded by the production runner. Reuse the ordinary deterministic
// fixture's C ABI/report implementation; none of the syscall outcomes is simulated.
#define PINEFORGE_HPO_FAKE_PLUGIN 1
#define run_backtest_full fixture_run_backtest_full
#include "test_engine_adapter.cpp"
#undef run_backtest_full

#include <chrono>
#include <condition_variable>
#include <fcntl.h>
#include <linux/sched.h>
#include <mutex>
#include <signal.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

namespace {

constexpr int child_exit = 42;
std::mutex gate_mutex;
std::condition_variable gate_ready;
unsigned gate_count = 0;
std::mutex exec_mutex;

void emit_probe(const char* operation, const char* phase, long result, int error, int status) {
    const char* descriptor = std::getenv("PFH_SECCOMP_PROBE_FD");
    if (!descriptor)
        ::_exit(90);
    char line[256];
    const int size = std::snprintf(line, sizeof(line), "%s %s %ld %ld %d %d\n",
                                   operation, phase, ::syscall(SYS_gettid),
                                   result, error, status);
    if (size <= 0 || static_cast<std::size_t>(size) >= sizeof(line))
        ::_exit(97);
    ssize_t written;
    do {
        written = ::write(std::atoi(descriptor), line, static_cast<std::size_t>(size));
    } while (written < 0 && errno == EINTR);
    if (written != size)
        ::_exit(91);
}

void probe(const char* phase) {
    const char* operation = std::getenv("PFH_SECCOMP_PROBE");
    const char* requested_phase = std::getenv("PFH_SECCOMP_PHASE");
    if (!operation || !requested_phase ||
        (std::strcmp(requested_phase, "all") != 0 && std::strcmp(requested_phase, phase) != 0))
        return;

    // The first two trials must reach the plugin concurrently. A serialized/one-worker
    // implementation times out here instead of making worker-inheritance checks vacuous.
    if (std::strcmp(phase, "worker") == 0) {
        std::unique_lock<std::mutex> lock(gate_mutex);
        ++gate_count;
        gate_ready.notify_all();
        if (!gate_ready.wait_for(lock, std::chrono::seconds(5), [] { return gate_count >= 2; }))
            ::_exit(96);
    }

    long result = -1;
    int status = 0;
    std::unique_lock<std::mutex> exec_lock;
    errno = 0;
    if (std::strcmp(operation, "fork") == 0) {
        result = ::fork();
        if (result == 0)
            ::_exit(child_exit);
    } else if (std::strcmp(operation, "vfork") == 0) {
        // Only the pid_t return slot may be modified by the child on the shared stack.
        // Store into the general result slot after the child has exited.
        const pid_t child = ::vfork();
        if (child == 0)
            ::_exit(child_exit);
        result = child;
#ifdef SYS_fork
    } else if (std::strcmp(operation, "raw_fork") == 0) {
        result = ::syscall(SYS_fork);
        if (result == 0)
            ::_exit(child_exit);
#endif
    } else if (std::strcmp(operation, "clone") == 0 ||
               std::strcmp(operation, "clone_high_flags") == 0) {
        // No shared VM/stack: a raw, fork-like clone. High bits catch a filter that tests
        // equality with zero instead of masking CLONE_THREAD in the low flags word.
        const unsigned long flags = SIGCHLD |
            (std::strcmp(operation, "clone_high_flags") == 0 ? (1UL << 32) : 0UL);
        result = ::syscall(SYS_clone, flags, nullptr, nullptr, nullptr, 0UL);
        if (result == 0)
            ::_exit(child_exit);
    } else if (std::strcmp(operation, "clone3") == 0) {
#if defined(SYS_clone3) && defined(CLONE_ARGS_SIZE_VER0)
        struct clone_args arguments {};
        arguments.exit_signal = SIGCHLD;
        result = ::syscall(SYS_clone3, &arguments, sizeof(arguments));
        if (result == 0)
            ::_exit(child_exit);
#else
        ::_exit(95);
#endif
    } else if (std::strcmp(operation, "execve") == 0 ||
               std::strcmp(operation, "execveat") == 0) {
        // A successful exec replaces the entire process. Serialize attempts so another
        // worker cannot emit a losing concurrent-exec error in the positive control.
        // Under the filter each attempt returns, releases this lock and is recorded.
        exec_lock = std::unique_lock<std::mutex>(exec_mutex);
        const char* executable = std::getenv("PFH_SECCOMP_EXEC_HELPER");
        if (!executable)
            ::_exit(92);
        char* arguments[] = {const_cast<char*>(executable),
                             const_cast<char*>("exec-success"), nullptr};
        if (std::strcmp(operation, "execve") == 0)
            result = ::syscall(SYS_execve, executable, arguments, ::environ);
        else
            result = ::syscall(SYS_execveat, AT_FDCWD, executable, arguments, ::environ, 0);
        // Success replaces the runner; the helper emits the positive control's record.
    } else if (std::strcmp(operation, "process_vm_readv") == 0 ||
               std::strcmp(operation, "process_vm_writev") == 0) {
        char source = 'A';
        char destination = 'B';
        struct iovec local {&destination, 1};
        struct iovec remote {&source, 1};
        result = ::syscall(std::strcmp(operation, "process_vm_readv") == 0
                               ? SYS_process_vm_readv : SYS_process_vm_writev,
                           ::getpid(), &local, 1UL, &remote, 1UL, 0UL);
        if (result == 1 && (std::strcmp(operation, "process_vm_readv") == 0
                               ? destination != 'A' : source != 'B'))
            ::_exit(98);
    } else if (std::strcmp(operation, "ptrace") == 0) {
        result = ::syscall(SYS_ptrace, PTRACE_TRACEME, 0UL, nullptr, nullptr);
    } else if (std::strcmp(operation, "threads") == 0) {
        // Actually create a plugin thread in both dlopen and worker phases. Merely
        // observing the runner's worker TIDs would miss plugin-side pthread regressions.
        std::thread child([&] { result = ::syscall(SYS_gettid); });
        child.join();
#if defined(__x86_64__) && !defined(__ILP32__)
    } else if (std::strcmp(operation, "x32") == 0) {
        result = ::syscall(__X32_SYSCALL_BIT | SYS_getpid);
    } else if (std::strcmp(operation, "compat") == 0) {
        // pidfd_send_signal uses the same header-defined number in both Linux x86 ABIs.
        // An invalid fd makes the off control harmless (EBADF, or ENOSYS if the compat
        // entry is unavailable). int 0x80 enters the i386 audit ABI from this LP64 process.
#ifdef SYS_pidfd_send_signal
        asm volatile("int $0x80"
                     : "=a"(result)
                     : "0"(static_cast<long>(SYS_pidfd_send_signal)), "b"(-1L), "c"(0L),
                       "d"(0L), "S"(0L)
                     : "memory", "cc", "r8", "r9", "r10", "r11");
        result = static_cast<std::int32_t>(result);
        if (result < 0) {
            errno = static_cast<int>(-result);
            result = -1;
        }
#else
        ::_exit(95);
#endif
#endif
    } else {
        ::_exit(93);
    }
    const int error = result < 0 ? errno : 0;
    const bool created_child = std::strcmp(operation, "fork") == 0 ||
        std::strcmp(operation, "vfork") == 0 || std::strcmp(operation, "raw_fork") == 0 ||
        std::strcmp(operation, "clone") == 0 ||
        std::strcmp(operation, "clone_high_flags") == 0 || std::strcmp(operation, "clone3") == 0;
    if (result > 0 && created_child) {
        pid_t waited;
        do {
            waited = ::waitpid(static_cast<pid_t>(result), &status, 0);
        } while (waited < 0 && errno == EINTR);
        if (waited != result || !WIFEXITED(status) || WEXITSTATUS(status) != child_exit)
            ::_exit(94);
        status = WEXITSTATUS(status);
    }
    emit_probe(operation, phase, result, error, status);
}

struct Initializer {
    Initializer() { probe("constructor"); }
} initializer;

}  // namespace

extern "C" PF_API void run_backtest_full(
    pf_strategy_t strategy, pf_bar_t* bars, int bar_count,
    const char* input_timeframe, const char* script_timeframe, int bar_magnifier,
    int magnifier_samples, pf_magnifier_distribution_t magnifier_distribution,
    pf_report_t* report) {
    probe("worker");
    fixture_run_backtest_full(strategy, bars, bar_count, input_timeframe, script_timeframe,
                             bar_magnifier, magnifier_samples, magnifier_distribution, report);
}
