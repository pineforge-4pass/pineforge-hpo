"""Small native-runner completions that honor the wrapper's redirected stdout."""

from __future__ import annotations

import subprocess
from typing import BinaryIO, Callable, Sequence


def native_completion(
    completed: subprocess.CompletedProcess,
) -> Callable[..., subprocess.CompletedProcess]:
    def run(
        command: Sequence[str], stdout: BinaryIO, progress_fd: int | None
    ) -> subprocess.CompletedProcess:
        stdout.write((completed.stdout or "").encode("utf-8"))
        return subprocess.CompletedProcess(
            command, completed.returncode, stderr=completed.stderr or ""
        )

    return run
