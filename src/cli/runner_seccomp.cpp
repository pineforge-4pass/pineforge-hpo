#include "runner_seccomp.hpp"

#include <pineforge/hpo/error.hpp>

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#if defined(__linux__)
#if __has_include(<linux/audit.h>) && __has_include(<linux/filter.h>) && \
    __has_include(<linux/sched.h>) && __has_include(<linux/seccomp.h>) && \
    __has_include(<sys/prctl.h>) && __has_include(<sys/syscall.h>) && \
    __has_include(<sys/utsname.h>)
#define PFH_RUNNER_SECCOMP_HEADERS 1
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/sched.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#endif
#endif

namespace pineforge::hpo::cli {
namespace {

[[noreturn]] void refuse(const char* reason, const std::string& message) {
    throw TypedHpoError<>("hpo_seccomp_unavailable", {{"reason", reason}}, message);
}

}  // namespace

void install_runner_seccomp() {
#if !defined(__linux__)
    refuse("platform", "--seccomp requires Linux");
#elif !defined(__LP64__) || (!defined(__x86_64__) && !defined(__aarch64__)) || \
    !defined(__BYTE_ORDER__) || !defined(__ORDER_LITTLE_ENDIAN__) || \
    __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
    refuse("architecture", "--seccomp requires the Linux x86_64 or aarch64 little-endian LP64 ABI");
#elif !defined(PFH_RUNNER_SECCOMP_HEADERS) || !defined(PR_SET_NO_NEW_PRIVS) || \
    !defined(PR_SET_SECCOMP) || !defined(SECCOMP_MODE_FILTER) || \
    !defined(SECCOMP_RET_KILL_PROCESS) || \
    !defined(__NR_execve) || !defined(__NR_execveat) || !defined(__NR_clone) || \
    !defined(__NR_clone3) || !defined(__NR_ptrace) || !defined(__NR_process_vm_readv) || \
    !defined(__NR_process_vm_writev) || !defined(CLONE_THREAD) || \
    (defined(__x86_64__) && (!defined(AUDIT_ARCH_X86_64) || !defined(__X32_SYSCALL_BIT) || \
                           !defined(__NR_fork) || !defined(__NR_vfork))) || \
    (defined(__aarch64__) && !defined(AUDIT_ARCH_AARCH64))
    refuse("headers", "--seccomp requires Linux syscall, audit and seccomp filter headers");
#else
    // Before Linux 5.4, untagged legacy x32 syscall aliases could bypass native-number
    // rules even with the x32 bit check below. Refuse those kernels rather than embed a
    // second, numeric syscall table. aarch64 needs 4.14 for KILL_PROCESS semantics.
    // A version string we cannot inspect is also a refusal.
    struct utsname kernel {};
    unsigned major = 0;
    unsigned minor = 0;
    if (::uname(&kernel) != 0 || std::sscanf(kernel.release, "%u.%u", &major, &minor) != 2)
        refuse("kernel", "--seccomp cannot determine the running Linux kernel release");
#if defined(__x86_64__)
    if (major < 5 || (major == 5 && minor < 4))
        refuse("kernel", "--seccomp on x86_64 requires Linux 5.4 or newer");
    constexpr std::uint32_t audit_arch = AUDIT_ARCH_X86_64;
#else
    if (major < 4 || (major == 4 && minor < 14))
        refuse("kernel", "--seccomp on aarch64 requires Linux 4.14 or newer");
    constexpr std::uint32_t audit_arch = AUDIT_ARCH_AARCH64;
#endif
    // Default ALLOW preserves dynamic-loader, glibc, arena, I/O and threading paths. Every
    // syscall number comes from this build's Linux headers, never a typed numeric table.
    sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, audit_arch, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        // x32 shares AUDIT_ARCH_X86_64 but sets this bit in the syscall number.
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, __X32_SYSCALL_BIT, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#endif
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_execve, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_execveat, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
#ifdef __NR_fork
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_fork, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
#endif
#ifdef __NR_vfork
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_vfork, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
#endif
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_ptrace, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_process_vm_readv, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_process_vm_writev, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        // glibc versions that try clone3 for pthread_create retry clone on ENOSYS.
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_clone, 0, 3),
        // Both supported ABIs pass clone flags in args[0]; little endian puts the
        // CLONE_THREAD bit in this low word. Other flags must not bypass the mask.
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, CLONE_THREAD, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program {
        static_cast<unsigned short>(sizeof(instructions) / sizeof(instructions[0])), instructions
    };
    const auto syscall_failed = [](const char* reason, const char* operation) {
        const int error = errno;
        refuse(reason, std::string("--seccomp: ") + operation + ": " + std::strerror(error));
    };
    // Repeating NNP after the outer jailer set it is harmless; it cannot be cleared.
    if (::prctl(PR_SET_NO_NEW_PRIVS, 1UL, 0UL, 0UL, 0UL) != 0)
        syscall_failed("no_new_privs", "PR_SET_NO_NEW_PRIVS failed");
    // prepare() has not started HPO threads, including the wall-cap watchdog in run_study().
    // Later watchdog, writer, trial and TPE threads use CLONE_THREAD and inherit the filter.
    // No TSYNC or per-worker reinstallation is needed for this standalone runner.
    if (::prctl(PR_SET_SECCOMP, static_cast<unsigned long>(SECCOMP_MODE_FILTER),
                &program, 0UL, 0UL) != 0)
        syscall_failed("filter", "PR_SET_SECCOMP failed");
#endif
}

}  // namespace pineforge::hpo::cli
