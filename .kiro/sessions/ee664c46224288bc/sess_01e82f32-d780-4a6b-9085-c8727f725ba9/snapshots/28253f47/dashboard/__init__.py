"""EchoGuard local dashboard (Flask JSON API + offline vanilla-JS/canvas UI).

Start with ``python -m dashboard`` (or ``make dashboard``).
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """Lazy wrapper around :func:`dashboard.server.create_app`."""
    from .server import create_app as _create_app

    return _create_app(*args, **kwargs)
