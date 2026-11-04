"""Shared helper to run tests with an isolated os.environ.

Tests clear the environment so a developer's GOOPDL_* (or any other) variables
cannot leak into them. On Windows a truly empty environment also breaks the
interpreter: without USERPROFILE ``Path.home()`` raises, and without
SYSTEMROOT/WINDIR sockets, asyncio (``_overlapped``) and child processes fail
with WinError 10106. So on Windows those OS essentials survive the clearing.
On POSIX nothing survives: the result is exactly the old empty environment
plus ``extra``.
"""
import os
from unittest.mock import patch

_WINDOWS_ESSENTIALS = frozenset(name.upper() for name in (
    "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
    "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "ProgramData",
))


def isolated_env(extra=None):
    """Return ``patch.dict(os.environ, extra, clear=True)`` keeping Windows essentials.

    The platform is checked on every call so tests can patch ``os.name``. The
    result works as a decorator, a context manager or via ``start()``/``stop()``.
    """
    values = {}
    if os.name == "nt":
        values = {k: v for k, v in os.environ.items() if k.upper() in _WINDOWS_ESSENTIALS}
    values.update(extra or {})
    return patch.dict(os.environ, values, clear=True)
