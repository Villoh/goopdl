"""The Google account saved by `goopdl auth login`. Only the CLI imports this.

account.json holds the email and either the AAS token or
``"storage": "keyring"``; then the token is in the system keyring under the
service "goopdl" with the email as user name. keyring is imported on first
use only: using it scans entry points and may start the desktop's secret
service, so commands without a saved account never touch it.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from goopdl.auth import (
    DirectAuthConfigurationError,
    _atomic_pool_json,
    _checked_credentials,
    get_config_dir,
)

FILE = "account.json"
SERVICE = "goopdl"
_AGAIN = (
    "Run goopdl auth login again, or set GOOPDL_ACCOUNT_EMAIL and GOOPDL_AAS_TOKEN."
)


class AccountError(Exception):
    """The saved account is unusable; the message names no token or email."""


def path() -> Path:
    return get_config_dir() / FILE


def read() -> tuple[str, str | None] | None:
    """Return (email, aas_token) from account.json; None without a file.

    aas_token is None when the token is in the system keyring. Raises
    AccountError for a file that is unreadable, malformed or, on POSIX, not
    a regular file private to this user.
    """
    file = path()
    private = (
        f"Refusing to use {file}: it must be a regular file that only you can "
        f"read. Run: chmod 600 {file}"
    )
    try:
        fd = os.open(
            file,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise AccountError(private) from None
    record = data = None
    try:
        info = os.fstat(fd)
        if os.name != "nt" and (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise AccountError(private)
        with open(fd, encoding="utf-8", closefd=False) as stream:
            data = json.load(stream)
        if isinstance(data, dict) and data.get("v") == 1:
            email, aas_token = data.get("email"), data.get("aas_token")
            storage = data.get("storage")
            if storage is None and isinstance(email, str) and isinstance(aas_token, str):
                record = _checked_credentials(email, aas_token)
            elif (storage == "keyring" and aas_token is None
                  and isinstance(email, str) and email.strip()):
                record = (email.strip(), None)
    except (OSError, ValueError):  # JSON, UTF-8 and credential checks
        pass
    finally:
        data = email = aas_token = None
        os.close(fd)
    if record is None:
        raise AccountError(
            f"Saved account {file} is invalid. Run goopdl auth login again, "
            "or goopdl auth logout to remove it."
        )
    return record


def load() -> tuple[str, str] | None:
    """Return the saved (email, aas_token), reading the keyring if needed."""
    record = read()
    try:
        if record is None:
            return None
        if record[1] is not None:
            return record[0], record[1]
        return record[0], keyring_token(record[0])
    finally:
        record = None  # a traceback keeps this frame's locals


def keyring_token(email: str) -> str:
    """Return the AAS token saved in the system keyring for *email*."""
    stored, failure = _keyring("get_password", email)
    aas_token = message = None
    if failure is not None:
        message = (
            "Could not read the saved account's token from the system keyring "
            f"({failure}). {_AGAIN}"
        )
    elif not isinstance(stored, str) or not stored.strip():
        message = f"The saved account's token is not in the system keyring. {_AGAIN}"
    else:
        try:
            aas_token = _checked_credentials(email, stored)[1]  # type: ignore[index]
        except DirectAuthConfigurationError:
            message = f"The saved account's token in the system keyring is invalid. {_AGAIN}"
    del email, stored  # a traceback keeps this frame's locals
    if aas_token is None:
        raise AccountError(message)
    return aas_token


def save(
    email: str, aas_token: str | None, insecure: bool = False
) -> tuple[bool, str | None]:
    """Save the account; return (in the keyring, why the keyring was not used).

    Without *insecure*, the token goes to the system keyring when a
    read-back confirms it, else to account.json. Raises OSError when
    account.json cannot be written.
    """
    try:
        old = read()
    except AccountError:
        old = None
    stale = {old[0]} if old is not None and old[1] is None else set()
    old = None
    failure = None
    if not insecure:
        _, failure = _keyring("set_password", email, aas_token)
        if failure is None:
            stored, failure = _keyring("get_password", email)
            if failure is None and stored != aas_token:
                failure = ("backend returned nothing" if stored is None
                           else "backend returned another value")
            stored = None
    in_keyring = not insecure and failure is None
    record: dict[str, object] | None = {"v": 1, "email": email}
    record.update({"storage": "keyring"} if in_keyring else {"aas_token": aas_token})
    try:
        _atomic_pool_json(path(), record)
    except OSError:
        if in_keyring and email not in stale:
            _keyring("delete_password", email)
        raise
    finally:
        record = aas_token = None
    if in_keyring:
        stale.discard(email)
    elif not insecure:
        stale.add(email)  # the backend may have kept what it accepted
    for name in stale:
        _keyring("delete_password", name)
    return in_keyring, failure


def forget(email: str) -> str | None:
    """Delete the keyring token; return why that failed, None when it is gone."""
    _, failure = _keyring("delete_password", email)
    return None if failure == "PasswordDeleteError" else failure  # not found


def backend_name() -> str:
    try:
        import keyring  # pyright: ignore[reportMissingImports]

        return str(keyring.get_keyring().name)
    except Exception:
        return "unavailable"


def _keyring(function: str, *args: object) -> tuple[object, str | None]:
    """Call keyring.<function>(SERVICE, *args); return (result, failure).

    The failure is only the exception's class name: messages from backends
    may echo arguments.
    """
    try:
        import keyring  # pyright: ignore[reportMissingImports]

        return getattr(keyring, function)(SERVICE, *args), None
    except Exception as exc:
        return None, type(exc).__name__
