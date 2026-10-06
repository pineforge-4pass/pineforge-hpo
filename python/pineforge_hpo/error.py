"""Stable failure metadata, independent of human-readable exception text."""

from __future__ import annotations

from typing import Any


class HpoError(Exception):
    """Exception base preserving legacy types and exposing typed failure args.

    ``str(error)`` and ``error.args`` retain the legacy message contract.
    ``error.failure_args`` is the scalar JSON object used by the failure protocol.
    """

    code: str | None = "hpo_unclassified_error"
    origin = "hpo"

    @property
    def failure_args(self) -> dict[str, Any] | None:
        arguments = getattr(self, "_failure_args", {})
        return dict(arguments) if arguments is not None else None

    def with_failure(
        self, code: str | None, args: dict[str, Any] | None, origin: str = "hpo"
    ):
        self.code = code
        self._failure_args = dict(args) if args is not None else None
        self.origin = origin
        return self


def failure_document(error: BaseException, exit_code: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": False,
        "failure": {
            "origin": getattr(error, "origin", "hpo"),
            "code": getattr(error, "code", "hpo_unclassified_error"),
            "args": error.failure_args if isinstance(error, HpoError) else {},
            "exit_code": exit_code,
        },
    }
