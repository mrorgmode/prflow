"""The one exception type commands raise for expected failures.

``code`` is a stable machine identifier (it appears in ``--json`` output); ``message``
and ``hint`` are for humans and may change wording.
"""

from __future__ import annotations

from typing import Any


class PrflowError(Exception):
    def __init__(self, code: str, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            data["hint"] = self.hint
        if self.details:
            data["details"] = self.details
        return data
