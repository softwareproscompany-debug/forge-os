"""Small env-var helpers shared by channel providers."""

from __future__ import annotations

import os


def get_env(name: str, default: str = "") -> str:
    """Read an env var, stripped; ``default`` when unset."""
    return os.environ.get(name, default).strip()


def require_env(name: str) -> str:
    """Read an env var or raise a clear :class:`ValueError` naming it."""
    value = get_env(name)
    if not value:
        raise ValueError(
            f"{name} is not set. Set it in the environment to use this provider."
        )
    return value
