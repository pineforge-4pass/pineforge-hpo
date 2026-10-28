#pragma once

namespace pineforge::hpo::cli {

// Irreversible for this thread and its future descendants. Call on the native main thread
// before dlopen; later watchdog, writer, trial and TPE threads inherit the filter.
// Throws a typed HpoError on refusal. Existing unrelated threads are not synchronized.
void install_runner_seccomp();

}  // namespace pineforge::hpo::cli
