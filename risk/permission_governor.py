"""Single permission-governor contract without duplicated risk formulas."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class PermissionResult:
    allowed: bool
    reason: str = ""
    evidence: dict[str, Any] | None = None


class PermissionGovernor:
    """Adapter around the existing authoritative risk checker."""

    def __init__(self, checker: Callable[..., Any]):
        self._checker = checker

    def check(self, *args: Any, **kwargs: Any) -> Any:
        return self._checker(*args, **kwargs)
