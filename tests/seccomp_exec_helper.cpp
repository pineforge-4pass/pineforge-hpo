// Positive exec control and real inherited-filter failure injection. This is not an
// interposition stub in the native runner.
#include <cerrno>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

extern char** environ;

int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "syscall-numbers") == 0) {
        struct Entry { const char* name; long number; };
        const Entry entries[] = {
            {"execve", SYS_execve}, {"execveat", SYS_execveat}, {"clone", SYS_clone},
#ifdef SYS_clone3
            {"clone3", SYS_clone3},
#endif
#ifdef SYS_fork
            {"fork", SYS_fork},
#endif
#ifdef SYS_vfork
            {"vfork", SYS_vfork},
#endif
            {"ptrace", SYS_ptrace}, {"process_vm_readv", SYS_process_vm_readv},
            {"process_vm_writev", SYS_process_vm_writev},
#if defined(__x86_64__) && !defined(__ILP32__) && defined(SYS_pidfd_send_signal)
            {"pidfd_send_signal", SYS_pidfd_send_signal},
#endif
        };
        std::printf("{");
        const char* separator = "";
        for (const auto& entry : entries) {
            std::printf("%s\"%s\":%ld", separator, entry.name, entry.number);
            separator = ",";
        }
        std::printf("}\n");
        return 0;
    }
    if (argc == 2 && std::strcmp(argv[1], "exec-success") == 0) {
        const char* descriptor = std::getenv("PFH_SECCOMP_PROBE_FD");
        const char* operation = std::getenv("PFH_SECCOMP_PROBE");
        const char* phase = std::getenv("PFH_SECCOMP_PHASE");
        if (!descriptor || !operation || !phase)
            return 90;
        char line[256];
        const int size = std::snprintf(line, sizeof(line), "%s %s %ld 0 0 42\n",
                                       operation, phase, ::syscall(SYS_gettid));
        if (size <= 0 || static_cast<std::size_t>(size) >= sizeof(line))
            return 96;
        ssize_t written;
        do {
            written = ::write(std::atoi(descriptor), line, static_cast<std::size_t>(size));
        } while (written < 0 && errno == EINTR);
        return written == size ? 42 : 91;
    }
    if (argc < 4)
        return 92;
    const bool block_nnp = std::strcmp(argv[1], "block-no-new-privs") == 0;
    const bool block_kernel = std::strcmp(argv[1], "block-kernel") == 0;
    if (!block_nnp && !block_kernel && std::strcmp(argv[1], "block-filter") != 0)
        return 93;
    // Refuse uname or one of the runner's prctl operations. exec remains allowed so the
    // real CLI sees the failed prerequisite/installation, before loading the probe plugin.
    sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_uname, 0, 1),
        BPF_STMT(BPF_RET | BPF_K,
                 block_kernel ? (SECCOMP_RET_ERRNO | EPERM) : SECCOMP_RET_ALLOW),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_prctl, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K,
                 static_cast<unsigned>(block_nnp ? PR_SET_NO_NEW_PRIVS : PR_SET_SECCOMP), 0, 1),
        BPF_STMT(BPF_RET | BPF_K,
                 block_kernel ? SECCOMP_RET_ALLOW : (SECCOMP_RET_ERRNO | EPERM)),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program {
        static_cast<unsigned short>(sizeof(instructions) / sizeof(instructions[0])), instructions
    };
    if (::prctl(PR_SET_NO_NEW_PRIVS, 1UL, 0UL, 0UL, 0UL) != 0 ||
        ::prctl(PR_SET_SECCOMP, static_cast<unsigned long>(SECCOMP_MODE_FILTER),
                &program, 0UL, 0UL) != 0)
        return 94;
    ::execve(argv[2], argv + 2, environ);
    return 95;
}
