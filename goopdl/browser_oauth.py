"""Capture EmbeddedSetup's one-time OAuth token from an isolated browser."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Event
from typing import Any

import httpx
from websocket import (  # type: ignore[import-not-found]
    WebSocketException,
    WebSocketTimeoutException,
    create_connection,
)

EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"
_WAIT = Event()


class BrowserOAuthError(RuntimeError):
    """Browser launch or OAuth token capture failed."""


def capture_oauth_credentials(timeout: float = 300) -> tuple[str, str]:
    """Open EmbeddedSetup and return its signed-in email and oauth_token."""
    browser = _find_browser()
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="goopdl-browser-") as profile:
        try:
            process = subprocess.Popen(
                [
                    str(browser),
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-allow-origins={origin}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    EMBEDDED_SETUP_URL,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise BrowserOAuthError(f"Could not start browser: {browser}") from exc
        try:
            websocket_url = _wait_for_debugger(port, process)
            return _wait_for_oauth_credentials(
                websocket_url, origin, process, timeout=timeout
            )
        except (OSError, WebSocketException) as exc:
            raise BrowserOAuthError("Browser debugging connection failed.") from exc
        finally:
            _stop_browser(process)


def _find_browser() -> Path:
    configured = os.environ.get("GOOPDL_BROWSER", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
        raise BrowserOAuthError(f"GOOPDL_BROWSER does not exist: {path}")

    for candidate in _browser_candidates():
        if candidate.is_file():
            return candidate
    raise BrowserOAuthError(
        "Chrome, Chromium, Edge, or Brave was not found. Set GOOPDL_BROWSER "
        "to the browser executable."
    )


def _browser_candidates() -> list[Path]:
    names = (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "brave-browser",
    )
    candidates = [Path(path) for name in names if (path := shutil.which(name))]

    if sys.platform == "win32":
        roots = [
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        ]
        relative_paths = (
            "Google/Chrome/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
            "BraveSoftware/Brave-Browser/Application/brave.exe",
        )
        candidates.extend(
            Path(root) / relative
            for root in roots
            if root
            for relative in relative_paths
        )
    elif sys.platform == "darwin":
        candidates.extend(
            Path(path)
            for path in (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            )
        )
    return candidates


def _free_port() -> int:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except (OSError, TypeError, ValueError) as exc:
        raise BrowserOAuthError("Could not allocate a local debugging port.") from exc


def _wait_for_debugger(
    port: int, process: subprocess.Popen[Any], timeout: float = 15
) -> str:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserOAuthError("Browser closed before sign-in started.")
            try:
                websocket_url = client.get(url).json().get("webSocketDebuggerUrl")
                if websocket_url:
                    return str(websocket_url)
            except (httpx.HTTPError, ValueError):
                pass
            _WAIT.wait(0.1)
    raise BrowserOAuthError("Browser debugging endpoint did not start.")


def _wait_for_oauth_credentials(
    websocket_url: str,
    origin: str,
    process: subprocess.Popen[Any],
    timeout: float,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    request_id = 0
    try:
        websocket = create_connection(
            websocket_url,
            origin=origin,
            timeout=2,
            http_no_proxy=["127.0.0.1", "localhost"],
        )
    except (OSError, WebSocketException) as exc:
        raise BrowserOAuthError("Could not connect to browser debugging.") from exc

    try:
        request_id, session_id = _attach_page(websocket, request_id)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserOAuthError(
                    "Browser closed before Google sign-in finished."
                )
            try:
                request_id, response = _cdp_request(
                    websocket, request_id, "Storage.getCookies"
                )
            except WebSocketTimeoutException:
                continue
            _raise_cdp_error(response, "cookie access")
            token = _oauth_token(response.get("result", {}).get("cookies", []))
            if token:
                request_id, response = _cdp_request(
                    websocket,
                    request_id,
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "document.querySelector('[data-profile-identifier]"
                            "[data-email]')?.getAttribute('data-email') || ''"
                        ),
                        "returnByValue": True,
                    },
                    session_id,
                )
                _raise_cdp_error(response, "account email access")
                email = _profile_email(response)
                if email:
                    return email, token
            _WAIT.wait(0.5)
    finally:
        websocket.close()
    raise BrowserOAuthError("Google sign-in timed out before completion.")


def _attach_page(websocket: Any, request_id: int) -> tuple[int, str]:
    request_id, response = _cdp_request(websocket, request_id, "Target.getTargets")
    _raise_cdp_error(response, "page discovery")
    targets = response.get("result", {}).get("targetInfos", [])
    pages = [target for target in targets if target.get("type") == "page"]
    target = next(
        (target for target in pages if "accounts.google.com" in target.get("url", "")),
        pages[0] if pages else None,
    )
    if not target:
        raise BrowserOAuthError("Google sign-in page was not found.")
    request_id, response = _cdp_request(
        websocket,
        request_id,
        "Target.attachToTarget",
        {"targetId": target["targetId"], "flatten": True},
    )
    _raise_cdp_error(response, "page attachment")
    session_id = response.get("result", {}).get("sessionId")
    if not session_id:
        raise BrowserOAuthError("Browser did not attach to Google sign-in page.")
    return request_id, str(session_id)


def _cdp_request(
    websocket: Any,
    request_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    request_id += 1
    message: dict[str, Any] = {"id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    if session_id is not None:
        message["sessionId"] = session_id
    websocket.send(json.dumps(message))
    return request_id, _receive_response(websocket, request_id)


def _raise_cdp_error(response: dict[str, Any], action: str) -> None:
    if error := response.get("error"):
        raise BrowserOAuthError(
            f"Browser rejected {action}: {error.get('message', 'unknown error')}"
        )


def _profile_email(response: dict[str, Any]) -> str | None:
    value = response.get("result", {}).get("result", {}).get("value")
    if not isinstance(value, str) or "@" not in value:
        return None
    return value.strip()


def _receive_response(websocket: Any, request_id: int) -> dict[str, Any]:
    while True:
        try:
            message = json.loads(websocket.recv())
        except (json.JSONDecodeError, TypeError):
            continue
        if message.get("id") == request_id:
            return message


def _oauth_token(cookies: list[dict[str, Any]]) -> str | None:
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".")
        value = str(cookie.get("value", ""))
        if (
            cookie.get("name") == "oauth_token"
            and (
                domain == "accounts.google.com"
                or domain.endswith(".accounts.google.com")
            )
            and value.startswith("oauth2_4/")
        ):
            return value
    return None


def _stop_browser(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
