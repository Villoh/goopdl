"""Typer CLI application — auth, download, info, search, list-splits."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import stat
import sys
import time
import webbrowser
from pathlib import Path
from typing import NoReturn

import httpx
import typer
from rich import print as rprint
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from goopdl import __version__
from goopdl.aastoken import AASTokenError, _AUTH_ERROR_CODES, fetch_aas_token
from goopdl.api import (
    AppNotAvailableError,
    AppNotSupportedError,
    AuthExpiredError,
    InvalidPackageError,
    MalformedResponseError,
    NetworkError,
    PlayAPIError,
    RateLimitedError,
    VersionUnavailableError,
    _TOKEN_FETCH_ATTEMPTS,
    _TOKEN_FETCH_COOLDOWN,
    _check_unique_dests,
    _safe_dest,
    download_app,
    fetch_app_item,
    get_delivery,
    get_details,
    purchase,
    search_apps,
    validate_package,
)
from goopdl.api import (
    list_splits as api_list_splits,
)
from goopdl.auth import (
    DEFAULT_PROBES,
    DirectAuthConfigurationError,
    DirectAuthError,
    DispenserRateLimitedError,
    DispenserUnavailableError,
    PoolBusyError,
    _atomic_pool_json,
    _checked_credentials,
    _env_credentials,
    _Scope,
    _use_scope,
    clear_auth,
    direct_auth_enabled,
    ensure_pool,
    fetch_token,
    get_config_dir,
    pick_pool_token,
    replace_pool_token,
    save_auth,
)
from goopdl.browser_oauth import (
    EMBEDDED_SETUP_URL,
    BrowserOAuthError,
    capture_oauth_credentials,
)
from goopdl.download import (
    DownloadError,
    DownloadSpec,
    IntegrityError,
    download_batch,
    write_manifest,
)
from goopdl.profiles import (
    VALID_ARCHS,
    find_profile,
    get_compat_profiles,
    get_discovery_profiles,
    get_latest_probe_profiles,
    get_priority_profiles,
)

console = Console()
err = Console(stderr=True)


class _StderrLogHandler(logging.Handler):
    """Show goopdl library log records on stderr, styled by level."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            style = "red"
        elif record.levelno >= logging.WARNING:
            style = "yellow"
        else:
            style = "dim"
        try:
            err.print(
                f"  {self.format(record)}", style=style, markup=False, highlight=False
            )
        except Exception:
            self.handleError(record)


def _setup_logging() -> None:
    log = logging.getLogger("goopdl")
    log.setLevel(logging.INFO)
    if not any(isinstance(h, _StderrLogHandler) for h in log.handlers):
        log.addHandler(_StderrLogHandler())


def _tolerate_unencodable_output() -> None:
    # Remote text (app titles, server messages) may not fit a legacy console
    # encoding such as cp1252; print "?" instead of crashing the command.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

# Distinct exit code so callers can tell "Google Play is unreachable right now"
# from "this artifact cannot be trusted" without parsing human-readable output.
AUTH_UNAVAILABLE_EXIT_CODE = 3
VERSION_UNAVAILABLE_EXIT_CODE = 4
RATE_LIMIT_EXIT_CODE = 5


app = typer.Typer(
    name="goopdl",
    help="Download APKs from Google Play Store.",
    add_completion=False,
    no_args_is_help=True,
    # Explicit: typer < 0.20 defaults to True and would print secret locals.
    pretty_exceptions_show_locals=False,
)


def _package_callback(value: str) -> str:
    try:
        return validate_package(value)
    except InvalidPackageError as exc:
        raise typer.BadParameter(str(exc)) from None


def _arch_callback(value: str) -> str:
    if value not in VALID_ARCHS:
        raise typer.BadParameter(
            f"Unknown architecture: {value}. Choose from {', '.join(VALID_ARCHS)}."
        )
    return value


def _version_callback(value: bool) -> None:
    if value:
        rprint(f"goopdl [bold]{__version__}[/bold]")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """GPlay APK Downloader — download APKs from Google Play Store."""
    _tolerate_unencodable_output()
    _setup_logging()
    if ctx.invoked_subcommand in {
        "latest",
        "info",
        "search",
        "list-splits",
        "download",
        "fast-download",
        "inspect-delivery",
    }:
        _use_credentials(ctx)


def _use_credentials(ctx: typer.Context) -> None:
    """Validate GOOPDL_*; without them, apply the account saved by auth login."""
    credentials = message = None
    try:
        if direct_auth_enabled():
            return
        credentials = _load_account()
    except (DirectAuthConfigurationError, _AccountFileError) as exc:
        message = str(exc)
    if message is not None:
        err.print(f"[red]{escape(message)}[/red]")
        raise typer.Exit(code=2)
    if credentials is not None:
        # Until the command ends, like a Client's credentials.
        ctx.with_resource(
            _use_scope(_Scope(credentials=credentials, explicit_credentials=True))
        )


# ── auth ────────────────────────────────────────────────────────────────────

auth_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")


@auth_app.callback()
def auth_main(ctx: typer.Context) -> None:
    """Sign in with a Google account, sign out, check status or fetch a token."""
    if ctx.invoked_subcommand == "token":
        _use_credentials(ctx)


@auth_app.command()
def login(
    print_token: bool = typer.Option(
        False, "--print", help="Print the AAS token to stdout instead of saving it."
    ),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Use your usual browser and paste the oauth_token cookie yourself.",
    ),
) -> None:
    """Sign in to a Google account in the browser and save it for goopdl."""
    failure = None
    try:
        return _run_login(print_token, manual)
    except BaseException as exc:
        failure = exc.with_traceback(None)
        failure.__cause__ = failure.__context__ = None
    # Click must not retain the original authentication exception or its frames.
    raise failure from None


@auth_app.command()
def logout() -> None:
    """Remove the saved Google account and every cached token."""
    path = _account_path()
    try:
        path.unlink()
        rprint(f"Removed the saved account {escape(str(path))}")
    except FileNotFoundError:
        rprint("No saved account.")
    except OSError as exc:
        err.print(
            f"[red]Could not remove {escape(str(path))}: "
            f"{escape(exc.strerror or type(exc).__name__)}[/red]"
        )
        raise typer.Exit(code=1) from None
    clear_auth()
    rprint("[green]All cached tokens removed.[/green]")
    if _account_env_set():
        err.print(
            "[yellow]GOOPDL_ACCOUNT_EMAIL/GOOPDL_AAS_TOKEN are still set, so goopdl "
            "keeps using that account. Unset them to sign out.[/yellow]"
        )


@auth_app.command()
def status() -> None:
    """Show which credentials goopdl uses. Never prints tokens."""
    try:
        environment = _env_credentials()
    except DirectAuthConfigurationError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from None
    def show(text: str) -> None:
        # Paths must not wrap: the output is read by people and scripts.
        console.print(text, soft_wrap=True)

    path = _account_path()
    saved = problem = None
    try:
        saved = _load_account()
    except _AccountFileError as exc:
        problem = str(exc)
    email = (environment or saved or (None,))[0]
    environment = saved = None  # drop the tokens

    if _account_env_set():
        mode = "environment (GOOPDL_ACCOUNT_EMAIL, GOOPDL_AAS_TOKEN)"
    elif email is not None:
        mode = "saved account"
    elif problem is not None:
        mode = "saved account (unusable)"
    else:
        mode = "anonymous (token dispenser)"
    show(f"Mode    : {mode}")
    if email is not None:
        show(f"Account : {escape(email)}")
    if path.exists() or problem is not None:
        note = " (ignored: the environment takes precedence)" if _account_env_set() else ""
        show(f"File    : {escape(str(path))}{note}")
    directory = get_config_dir()
    tokens = sum(1 for pattern in ("auth-*.json", "token-pool-*.json")
                 for _ in directory.glob(pattern))
    devices = sum(1 for _ in directory.glob("direct-device-*.json"))
    show(
        f"Cache   : {escape(str(directory))} ({tokens} token file(s), "
        f"{devices} device registration(s))"
    )
    if problem is not None:
        err.print(f"[yellow]{escape(problem)}[/yellow]")


@auth_app.command()
def token(
    arch: str = typer.Option(
        "arm64",
        help="Device type: arm64, armv7, x86, x86_64 or tv.",
        callback=_arch_callback,
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    country: str | None = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code to register device in that region (e.g. IN, JP, BR).",
    ),
    proxy: str | None = typer.Option(
        None, "--proxy", "-p", help="Proxy URL for dispenser + FDFE calls."
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Device profile key or name substring (e.g. 'Pv' or 'samsung'). Run 'goopdl profiles' to list all.",
    ),
) -> None:
    """Acquire a Google Play auth token.

    Without a Google account, use --country to register the device with a
    specific region's MCC/MNC, so subsequent info/download calls with the
    same --country get that region's catalog without needing a proxy.
    """
    direct = direct_auth_enabled()
    if not direct:
        rprint(f"[dim]Dispenser:[/dim] {dispenser or 'https://auroraoss.com/api/auth'}")
        rprint(f"[dim]Architecture:[/dim] {arch}")
        if country:
            rprint(f"[dim]Country:[/dim] {country.upper()}")
        rprint()

    status = (
        "Authenticating with Google..."
        if direct
        else "Rotating through device profiles..."
    )
    try:
        with console.status(status):
            data = fetch_token(
                dispenser_url=dispenser,
                arch=arch,
                country=country,
                proxy=proxy,
                profile=profile,
                raise_on_error=True,
            )
    except DirectAuthError as exc:
        _exit_token_error(exc)

    if not data:
        message = (
            "Authentication failed."
            if direct
            else "Authentication failed — all profiles rejected."
        )
        err.print(f"[red]{message}[/red]")
        raise typer.Exit(code=AUTH_UNAVAILABLE_EXIT_CODE) from None

    if direct:
        rprint("[bold green]Authenticated[/bold green]")
        return

    path = save_auth(data, arch, country, profile)
    rprint(
        Panel.fit(
            f"[bold green]Authenticated[/bold green]\n"
            f"Email  : {escape(str(data.get('email', 'N/A')))}\n"
            f"GSF ID : {escape(str(data.get('gsfId', 'N/A')))}\n"
            f"Saved  : {path}",
            title="Token",
        )
    )


@app.command(hidden=True)
def aastoken(
    args: list[str] | None = typer.Argument(None, hidden=True),
    oauth: bool = typer.Option(False, "--oauth", help="Same as auth login --manual."),
    browser: bool = typer.Option(False, "--browser", help="Same as auth login."),
) -> None:
    """Deprecated: use goopdl auth login --print."""
    err.print("[yellow]goopdl aastoken is deprecated, use `goopdl auth login --print`.[/yellow]")
    if args or (oauth and browser):
        args = None  # may hold a password
        err.print("[red]Use only --browser or --oauth; passwords are not supported.[/red]")
        raise typer.Exit(code=2)
    login(print_token=True, manual=oauth)


def _run_login(print_token: bool, manual: bool) -> None:
    if manual:
        email, oauth_token = _prompt_oauth_credentials()
    else:
        err.print(
            "Sign in to Google in the new browser window and select [bold]I agree[/bold]."
        )
        try:
            email, oauth_token = capture_oauth_credentials()
        except BrowserOAuthError:
            err.print(
                "[red]Browser sign-in failed.[/red] Set GOOPDL_BROWSER to a Chrome, "
                "Edge, Chromium or Brave executable, or run "
                "[bold]goopdl auth login --manual[/bold]."
            )
            raise typer.Exit(code=1) from None

    try:
        aas_token = fetch_aas_token(email, oauth_token)
    except AASTokenError as exc:
        reason = str(exc) if str(exc) in _AUTH_ERROR_CODES else "unknown error"
        err.print(f"[red]Google account authentication failed:[/red] {reason}")
        if str(exc) == "BadAuthentication":
            err.print(
                "[yellow]The oauth_token works once and expires quickly. "
                "Run [bold]goopdl auth login[/bold] again.[/yellow]"
            )
        raise typer.Exit(code=1) from None
    except httpx.HTTPError:
        err.print("[red]Google authentication request failed.[/red]")
        raise typer.Exit(code=1) from None
    except ValueError:
        err.print("[red]Invalid Google account credentials.[/red]")
        raise typer.Exit(code=1) from None
    finally:
        oauth_token = None

    if print_token:
        # Plain echo: Rich would wrap the long token at the console width.
        typer.echo(aas_token)
        return
    message = None
    try:
        _checked_credentials(email, aas_token)
        path = _save_account(email, aas_token)
    except DirectAuthConfigurationError:
        message = "Google returned an unexpected token; nothing was saved."
    except OSError as exc:
        message = f"Could not save the account: {exc.strerror or type(exc).__name__}"
    finally:
        aas_token = None
    if message is not None:
        err.print(f"[red]{escape(message)}[/red]")
        raise typer.Exit(code=1)
    console.print(f"Logged in as [bold]{escape(email)}[/bold] ({escape(str(path))}).")
    console.print(
        "goopdl commands now use this account. See [bold]goopdl auth status[/bold]; "
        "sign out with [bold]goopdl auth logout[/bold]."
    )
    if _account_env_set():
        err.print(
            "[yellow]GOOPDL_ACCOUNT_EMAIL/GOOPDL_AAS_TOKEN are set and take "
            "precedence over the saved account.[/yellow]"
        )


def _prompt_oauth_credentials() -> tuple[str, str]:
    err.print(
        "1. Sign in to Google in the page that opens and select [bold]I agree[/bold].\n"
        f"   If no browser opens, visit {EMBEDDED_SETUP_URL}\n"
        "2. Open developer tools ([bold]F12[/bold]) → Application → Cookies → "
        "https://accounts.google.com\n"
        "3. Copy the [bold]oauth_token[/bold] cookie (it starts with oauth2_4/) "
        "and paste it below. It works once and expires quickly."
    )
    _open_browser(EMBEDDED_SETUP_URL)
    # Prompts stay off stdout: click's visible prompt writes its last character there.
    email = err.input("Email: ").strip()
    oauth_token = typer.prompt("oauth_token", hide_input=True, err=True).strip()
    if not oauth_token.startswith("oauth2_4/"):
        oauth_token = None
        err.print("[red]The oauth_token must start with oauth2_4/.[/red]")
        raise typer.Exit(code=2)
    return email, oauth_token


def _open_browser(url: str) -> None:
    """webbrowser.open with stdout pointed at stderr.

    A browser started here inherits stdout; in TOKEN=$(goopdl ...) it would
    print into the token and keep the shell waiting until it exits.
    """
    try:
        sys.stdout.flush()
        saved = os.dup(1)
    except (OSError, ValueError):
        webbrowser.open(url)
        return
    try:
        os.dup2(2, 1)
        webbrowser.open(url)
    except OSError:
        webbrowser.open(url)
    finally:
        os.dup2(saved, 1)
        os.close(saved)


# Only the CLI reads account.json; the library and Client never do.
ACCOUNT_FILE = "account.json"


class _AccountFileError(Exception):
    """account.json is unusable; the message names only its path."""


def _account_path() -> Path:
    return get_config_dir() / ACCOUNT_FILE


def _account_env_set() -> bool:
    return any(
        os.environ.get(name, "").strip()
        for name in ("GOOPDL_ACCOUNT_EMAIL", "GOOPDL_AAS_TOKEN")
    )


def _save_account(email: str, aas_token: str) -> Path:
    """Write account.json atomically, 0600 in the 0700 config directory."""
    path = _account_path()
    _atomic_pool_json(path, {"v": 1, "email": email, "aas_token": aas_token})
    return path


def _load_account() -> tuple[str, str] | None:
    """Return the saved (email, aas_token); None without a file.

    Raises _AccountFileError for a file that is unreadable, malformed or, on
    POSIX, not a regular file private to this user.
    """
    path = _account_path()
    private = (
        f"Refusing to use {path}: it must be a regular file that only you can "
        f"read. Run: chmod 600 {path}"
    )
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise _AccountFileError(private) from None
    credentials = data = None
    try:
        info = os.fstat(fd)
        if os.name != "nt" and (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise _AccountFileError(private)
        with open(fd, encoding="utf-8", closefd=False) as stream:
            data = json.load(stream)
        if isinstance(data, dict) and data.get("v") == 1:
            email, aas_token = data.get("email"), data.get("aas_token")
            if isinstance(email, str) and isinstance(aas_token, str):
                credentials = _checked_credentials(email, aas_token)
    except (OSError, ValueError):  # JSON, UTF-8 and credential checks
        pass
    finally:
        data = email = aas_token = None
        os.close(fd)
    if credentials is None:
        raise _AccountFileError(
            f"Saved account {path} is invalid. Run goopdl auth login again, "
            "or goopdl auth logout to remove it."
        )
    return credentials


# ── profiles ────────────────────────────────────────────────────────────────


@app.command()
def profiles(
    arch: str = typer.Option(
        "all", help="Filter by device type: arm64, armv7, x86, x86_64, tv or all."
    ),
) -> None:
    """List available device profiles."""
    archs = VALID_ARCHS if arch == "all" else tuple(_parse_archs(arch))
    pool: list[tuple[str, dict, str]] = []
    seen: set[tuple[str, str]] = set()
    for arch_item in archs:
        for key, profile in get_priority_profiles(arch_item):
            if (key, arch_item) not in seen:
                seen.add((key, arch_item))
                pool.append((key, profile, arch_item))

    table = Table(title="Device Profiles")
    table.add_column("Key", style="bold", width=6)
    table.add_column("Name")
    table.add_column("Arch", style="dim", width=8)
    table.add_column("Android", style="dim", width=8)
    for key, p, a in pool:
        table.add_row(
            key, p.get("UserReadableName", key), a, p.get("Build.VERSION.RELEASE", "?")
        )
    console.print(table)


# ── latest ──────────────────────────────────────────────────────────────────


@app.command()
def latest(
    package: str = typer.Argument(
        ..., help="Package name (e.g. com.whatsapp).", callback=_package_callback
    ),
    arch: str = typer.Option(
        "arm64", help="Architecture for token.", callback=_arch_callback
    ),
    country: str | None = typer.Option(
        None, "--country", "-c", help="2-letter country code."
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    stable: int = typer.Option(
        3,
        "--stable",
        "-s",
        help="Stop early when max version unchanged for this many consecutive probes (default 3).",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Device profile for all probes (e.g. 'Galaxy S25 Ultra'). Defaults to top-ranked.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        "-p",
        help="Proxy URL for dispenser + FDFE calls, e.g. socks5://host:port.",
    ),
) -> None:
    """Find the latest available version by probing the regional GSF ID pool."""
    if profile:
        found = find_profile(profile, arch)
        if not found:
            err.print(f"[red]Profile not found: {profile}[/red]")
            raise typer.Exit(code=1) from None
        profile_key, profile_data = found
    else:
        top = get_latest_probe_profiles(arch, n=1)
        if not top:
            err.print("[red]No profiles available.[/red]")
            raise typer.Exit(code=1) from None
        profile_key, profile_data = top[0]

    device_name = profile_data.get("UserReadableName", profile_key)
    rprint(
        f"[dim]Profile:[/dim] {device_name}  [dim]Pool size:[/dim] {DEFAULT_PROBES}  [dim]Stable threshold:[/dim] {stable}"
    )

    # Ensure regional pool has DEFAULT_PROBES tokens — only hits dispenser for deficit
    try:
        tokens = ensure_pool(
            arch=arch,
            country=country,
            proxy=proxy,
            dispenser_url=dispenser,
            profile=profile_key,
            raise_on_error=True,
        )
    except PoolBusyError:
        _exit_pool_busy(1)
    except DispenserUnavailableError as exc:
        if exc.reason != "profile_rejected":
            _exit_token_error(exc)
        tokens = []
    except DirectAuthError as exc:
        _exit_token_error(exc)
    if not tokens:
        err.print(
            "[red]Could not obtain any tokens for this profile.[/red]"
        )
        raise typer.Exit(code=1) from None

    results: list[tuple[str, str, int]] = []
    best_vc = 0
    consecutive_stable = 0

    for i, token in enumerate(tokens):
        gsf_prefix = str(token.get("gsfId", "?"))[:8]
        try:
            details = get_details(package, token, country=country, proxy=proxy)
        except AuthExpiredError:
            err.print(
                f"[dim]  probe {i + 1}: {gsf_prefix}... token expired — replacing[/dim]"
            )
            try:
                new_token = replace_pool_token(
                    token,
                    arch=arch,
                    country=country,
                    proxy=proxy,
                    dispenser_url=dispenser,
                    profile=profile_key,
                    raise_on_error=True,
                )
            except PoolBusyError:
                new_token = None  # another process owns the pool; skip this probe
            except DirectAuthError as exc:
                if exc.status == 429:
                    _exit_token_error(exc)
                # The failed token is gone; remaining probes are still valid.
                new_token = None
            # A survivor (dispenser 429) is already one of the probes.
            if new_token is None or new_token in tokens:
                err.print(
                    f"[dim]  probe {i + 1}: replacement unavailable, skipping[/dim]"
                )
                continue
            tokens[i] = new_token
            try:
                details = get_details(package, new_token, country=country, proxy=proxy)
            except (RateLimitedError, NetworkError, MalformedResponseError) as exc:
                _exit_transient_play_error(exc)
            except PlayAPIError as exc:
                err.print(f"[dim]  probe {i + 1}: retry failed: {escape(str(exc))}[/dim]")
                continue
        except (RateLimitedError, NetworkError, MalformedResponseError) as exc:
            _exit_transient_play_error(exc)
        except PlayAPIError as exc:
            err.print(f"[dim]  probe {i + 1}: {gsf_prefix}... error: {escape(str(exc))}[/dim]")
            continue

        vc = details.version_code
        vs = details.version_string
        results.append((gsf_prefix, vs, vc))

        if vc > best_vc:
            best_vc = vc
            consecutive_stable = 0
            rprint(
                f"  probe {i + 1}/{len(tokens)}: gsf={gsf_prefix}... {escape(vs)} ({vc}) [bold green]^ new max[/bold green]"
            )
        else:
            consecutive_stable += 1
            rprint(
                f"  probe {i + 1}/{len(tokens)}: gsf={gsf_prefix}... {escape(vs)} ({vc}) [dim](stable {consecutive_stable}/{stable})[/dim]"
            )

        if consecutive_stable >= stable:
            rprint(f"[dim]  Converged after {i + 1} probes.[/dim]")
            break

    if not results:
        err.print("[red]Could not fetch version from any probe.[/red]")
        raise typer.Exit(code=1) from None

    results.sort(key=lambda x: x[2], reverse=True)
    _, best_version, best_vc_final = results[0]

    table = Table(title=f"Probe Results: {package}", show_header=True)
    table.add_column("GSF prefix", style="dim")
    table.add_column("Version")
    table.add_column("Version Code", justify="right")
    for gsf, vs, vc in results:
        style = "bold green" if vc == best_vc_final else ""
        table.add_row(gsf + "...", escape(vs), str(vc), style=style)
    console.print(table)

    rprint(
        f"\n[bold green]Latest:[/bold green] {escape(best_version)} ({best_vc_final})"
        + (f"  [dim]Region: {country.upper()}[/dim]" if country else "")
    )


# ── info ────────────────────────────────────────────────────────────────────


@app.command()
def info(
    package: str = typer.Argument(
        ..., help="Package name (e.g. com.whatsapp).", callback=_package_callback
    ),
    arch: str = typer.Option(
        "arm64", help="Architecture for token.", callback=_arch_callback
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    country: str | None = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code (e.g. US, IN, DE). Sets gl= and locale headers. For true regional versions, combine with --proxy.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        "-p",
        help="Proxy URL for FDFE calls, e.g. socks5://host:port or http://host:port. Routes requests through a regional IP.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Device profile key or name substring (e.g. 'D2' or 'samsung'). Run 'goopdl profiles' to list all.",
    ),
    raw: bool = typer.Option(
        False, "--raw", "-r", help="Print full decoded protobuf response as JSON."
    ),
) -> None:
    """Show app details from Google Play.

    Use --country to set region headers, and --proxy to route through a
    regional IP (required for apps with region-specific version tracks).
    Use --raw to dump the full decoded protobuf response as JSON.
    """
    import json as _json

    item: dict | None = None
    details = None
    auth_data = None
    with console.status(f"Fetching details for [bold]{package}[/bold]..."):
        try:
            if raw:
                item = fetch_app_item(
                    package,
                    arch=arch,
                    country=country,
                    proxy=proxy,
                    profile=profile,
                    dispenser_url=dispenser,
                )
            else:
                auth_data = _require_auth(
                    arch, dispenser, country=country, proxy=proxy, profile=profile
                )
                try:
                    details = get_details(
                        package, auth_data, country=country, proxy=proxy
                    )
                except AuthExpiredError:
                    auth_data = replace_pool_token(
                        auth_data,
                        arch=arch,
                        country=country,
                        proxy=proxy,
                        dispenser_url=dispenser,
                        profile=profile,
                    )
                    if not auth_data:
                        raise PlayAPIError("Token expired and replacement failed.")
                    details = get_details(
                        package, auth_data, country=country, proxy=proxy
                    )
        except DirectAuthError as exc:
            _exit_token_error(exc)
        except PlayAPIError as exc:
            if isinstance(exc.__cause__, DispenserRateLimitedError):
                _exit_token_error(exc.__cause__)  # fetch_app_item (--raw)
            err.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None

    if raw:
        assert item is not None
        print(_json.dumps(item, indent=2, ensure_ascii=False))
        return

    assert details is not None and auth_data is not None
    table = Table(
        title=escape(details.title or package), show_header=False, title_style="bold"
    )
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Package", escape(details.package))
    table.add_row(
        "Version", f"{escape(details.version_string)} ({details.version_code})"
    )
    if country:
        table.add_row("Region", country.upper())
    table.add_row("Device", escape(_device_label(auth_data)))
    table.add_row("Developer", escape(details.developer or "N/A"))
    table.add_row("Rating", escape(details.rating or "N/A"))
    table.add_row("Downloads", escape(details.downloads or "N/A"))
    table.add_row("Play Store", escape(details.play_url))
    console.print(table)


# ── search ──────────────────────────────────────────────────────────────────


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results."),
    arch: str = typer.Option(
        "arm64", help="Architecture for token.", callback=_arch_callback
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    country: str | None = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code for regional search results.",
    ),
    proxy: str | None = typer.Option(
        None, "--proxy", "-p", help="Proxy URL for FDFE calls."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Device profile key or name substring."
    ),
) -> None:
    """Search for apps on Google Play."""
    auth_data = _require_auth(
        arch, dispenser, country=country, proxy=proxy, profile=profile
    )

    with console.status(f"Searching for [bold]{query}[/bold]..."):
        try:
            try:
                results = search_apps(
                    query, auth_data, limit=limit, country=country, proxy=proxy
                )
            except AuthExpiredError:
                auth_data = _require_auth(
                    arch,
                    dispenser,
                    force=True,
                    country=country,
                    proxy=proxy,
                    profile=profile,
                )
                results = search_apps(
                    query, auth_data, limit=limit, country=country, proxy=proxy
                )
        except PlayAPIError as exc:
            err.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None

    if not results:
        rprint("[yellow]No results found.[/yellow]")
        raise typer.Exit()

    table = Table(title=f'Results for "{escape(query)}"')
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("Package")
    for i, app_item in enumerate(results, 1):
        table.add_row(str(i), escape(app_item["title"]), escape(app_item["package"]))
    console.print(table)


# ── list-splits ─────────────────────────────────────────────────────────────


@app.command("list-splits")
def list_splits_cmd(
    package: str = typer.Argument(
        ..., help="Package name.", callback=_package_callback
    ),
    arch: str = typer.Option(
        "arm64", help="Architecture for token.", callback=_arch_callback
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
) -> None:
    """List available split APKs for an app."""
    auth_data = _require_auth(arch, dispenser)

    with console.status(f"Fetching splits for [bold]{package}[/bold]..."):
        try:
            try:
                splits = api_list_splits(package, auth_data)
            except AuthExpiredError:
                auth_data = _require_auth(arch, dispenser, force=True)
                splits = api_list_splits(package, auth_data)
        except PlayAPIError as exc:
            err.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None

    if not splits:
        rprint(f"[yellow]{package} has no split APKs.[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Splits for {package}")
    table.add_column("#", style="dim", width=4)
    table.add_column("Split name")
    for i, name in enumerate(splits, 1):
        table.add_row(str(i), escape(name))
    console.print(table)
    rprint(f"\n[dim]Total: {len(splits)} splits[/dim]")


# ── download ────────────────────────────────────────────────────────────────


def _parse_archs(value: str) -> list[str]:
    archs = list(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )
    invalid = [item for item in archs if item not in VALID_ARCHS]
    if not archs or invalid:
        choices = ", ".join(VALID_ARCHS)
        raise typer.BadParameter(
            f"Unknown architecture: {', '.join(invalid) or value}. Choose from {choices}."
        )
    return archs


def _adb_install_command(specs: list[DownloadSpec]) -> str:
    """Quote selected installable files for POSIX shells or Windows PowerShell."""
    paths = [str(spec.dest) for spec in specs if spec.dest.suffix in {".apk", ".dm"}]
    if os.name == "nt":
        # PowerShell also treats U+2018–U+201B as single-quote delimiters.
        return "adb install-multiple " + " ".join(
            "'" + re.sub(r"['\u2018-\u201b]", lambda m: m.group() * 2, path) + "'"
            for path in paths
        )
    return shlex.join(["adb", "install-multiple", *paths])


def _parse_locales(value: str | None) -> list[str] | None:
    if not value:
        return None
    return list(
        dict.fromkeys(
            ["en-US"]
            + [
                item.strip().replace("_", "-")
                for item in value.split(",")
                if item.strip()
            ]
        )
    )


def _acquire_delivery(
    package: str,
    version_code: int | None,
    arch: str,
    auth_data: dict,
    dispenser: str | None,
    country: str | None,
    proxy: str | None,
    profile: str | None,
    locales: list[str] | None = None,
):
    def flow(current_auth: dict):
        details = get_details(package, current_auth, country=country, proxy=proxy)
        vc = version_code if version_code is not None else details.version_code
        delivery_token = purchase(
            package, vc, current_auth, country=country, proxy=proxy
        )
        delivery = get_delivery(
            package,
            vc,
            current_auth,
            country=country,
            proxy=proxy,
            delivery_token=delivery_token,
            locales=locales,
        )
        return details, vc, delivery

    def retry_with(profiles):
        for key, _profile_data in profiles:
            retry_auth = _fetch_probe_token(
                dispenser_url=dispenser,
                arch=arch,
                proxy=proxy,
                country=country,
                profile=key,
            )
            if not retry_auth:
                continue
            try:
                return flow(retry_auth)
            except (RateLimitedError, NetworkError, MalformedResponseError):
                raise  # another profile would hit the same throttle/outage/bug
            except PlayAPIError:
                continue
        return None

    try:
        try:
            return flow(auth_data)
        except AuthExpiredError:
            replacement = replace_pool_token(
                auth_data,
                arch=arch,
                country=country,
                proxy=proxy,
                dispenser_url=dispenser,
                profile=profile,
            )
            if not replacement:
                raise PlayAPIError("Token expired and replacement failed.") from None
            return flow(replacement)
    except DispenserRateLimitedError as exc:
        raise RateLimitedError(str(exc), retry_after=exc.retry_after) from None
    except (AppNotSupportedError, AppNotAvailableError) as exc:
        profiles = (
            get_compat_profiles(arch)[:6]
            if isinstance(exc, AppNotSupportedError)
            else get_discovery_profiles(arch)
        )
        result = retry_with(profiles)
        if result:
            return result
        raise


@app.command("inspect-delivery")
def inspect_delivery_cmd(
    package: str = typer.Argument(
        ..., help="Package name.", callback=_package_callback
    ),
    version: int = typer.Option(..., "--version", "-v", help="Version code."),
    arch: str = typer.Option(
        "arm64", "--arch", help="Device architecture.", callback=_arch_callback
    ),
    dispenser: str | None = typer.Option(None, "--dispenser", "-d"),
    country: str | None = typer.Option(None, "--country", "-c"),
    proxy: str | None = typer.Option(None, "--proxy", "-p"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON output."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Write JSON to this file."
    ),
) -> None:
    """Inspect version delivery metadata without downloading APK files."""
    auth_data = _require_auth(
        arch, dispenser, country=country, proxy=proxy, profile=profile
    )
    try:
        details, version_code, delivery = _acquire_delivery(
            package,
            version,
            arch,
            auth_data,
            dispenser,
            country,
            proxy,
            profile,
        )
    except RateLimitedError:
        err.print(
            "[red]Google Play rate limited delivery requests (HTTP 429); "
            "retry later.[/red]"
        )
        raise typer.Exit(code=RATE_LIMIT_EXIT_CODE) from None
    except VersionUnavailableError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=VERSION_UNAVAILABLE_EXIT_CODE) from None
    except PlayAPIError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    # Play Store details describe the current listing, not necessarily this version.
    version_name = (
        details.version_string or None
        if details.version_code == version_code
        else None
    )
    payload = {
        "package": package,
        "architecture": arch,
        "version": {"name": version_name, "code": version_code},
        "splits": ["base", *(split.name for split in delivery.splits)],
    }
    encoded = json.dumps(payload, sort_keys=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
    if json_output and output is None:
        print(encoded)
    elif not json_output:
        rprint(
            f"[bold]{package}[/bold] {escape(version_name or 'version name unknown')} "
            f"({version_code}) \\[{arch}]"
        )
        for split in payload["splits"]:
            rprint(f"- {escape(split)}")


@app.command()
def download(
    package: str = typer.Argument(
        ..., help="Package name (e.g. com.whatsapp).", callback=_package_callback
    ),
    output: Path = typer.Option(".", "--output", "-o", help="Output directory."),
    arch: str = typer.Option(
        "arm64",
        "--arch",
        "-a",
        help="Device type: arm64, armv7, x86, x86_64 or tv. Comma-separate several.",
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Version code (e.g. 384009971) or version string (e.g. 434.0.0.44.74).",
    ),
    locale: str | None = typer.Option(
        None,
        "--locale",
        "-l",
        help="Extra language splits, comma-separated (e.g. de,fr,zh-CN).",
    ),
    dm: bool = typer.Option(
        False, "--dm", help="Download DEX metadata used to speed up first launch."
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    no_splits: bool = typer.Option(
        True, "--no-splits/--splits", help="Include split APKs (default: skipped)."
    ),
    no_extras: bool = typer.Option(
        True,
        "--no-extras/--extras",
        help="Include OBB / asset packs (default: skipped).",
    ),
    country: str | None = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code (e.g. IN, US). Use with --proxy for true regional APK variants.",
    ),
    proxy: str | None = typer.Option(
        None, "--proxy", "-p", help="Proxy URL for FDFE calls, e.g. socks5://host:port."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="Device profile key or name substring."
    ),
    integrity_manifest: Path | None = typer.Option(
        None,
        "--integrity-manifest",
        help="Write verified file metadata as JSON after all downloads pass.",
    ),
) -> None:
    """Download an APK (with splits + additional files) from Google Play."""
    specs: list[DownloadSpec] = []
    for arch_item in _parse_archs(arch):
        try:
            specs += _download_arch(
                package,
                output,
                arch_item,
                version,
                locale,
                dm,
                dispenser,
                no_splits,
                no_extras,
                country,
                proxy,
                profile,
                require_integrity=integrity_manifest is not None,
            )
        except PlayAPIError as exc:  # unsafe or colliding output file names
            err.print(str(exc), style="red", markup=False)
            raise typer.Exit(code=1) from None
    if integrity_manifest:
        # Only digest-verified files (no OBB/asset packs); one entry per path,
        # since archs can share files such as the base APK.
        verified = {s.dest: s for s in specs if s.integrity_required}
        write_manifest(integrity_manifest, list(verified.values()))


def _download_arch(
    package: str,
    output: Path,
    arch: str,
    version: str | None,
    locale: str | None,
    dm: bool,
    dispenser: str | None,
    no_splits: bool,
    no_extras: bool,
    country: str | None,
    proxy: str | None,
    profile: str | None,
    require_integrity: bool,
) -> list[DownloadSpec]:
    """Download one architecture's files and return their specs."""
    try:
        auth_data = pick_pool_token(
            arch=arch,
            country=country,
            proxy=proxy,
            dispenser_url=dispenser,
            profile=profile,
            raise_on_error=True,
        )
    except PoolBusyError:
        _exit_pool_busy(AUTH_UNAVAILABLE_EXIT_CODE)
    except DirectAuthError as exc:
        _exit_token_error(exc)
    if not auth_data:
        message = (
            "Direct Google authentication failed."
            if direct_auth_enabled()
            else "Could not obtain an auth token — dispenser may be rate-limiting."
        )
        err.print(f"[red]{message}[/red]")
        raise typer.Exit(code=AUTH_UNAVAILABLE_EXIT_CODE) from None

    output.mkdir(parents=True, exist_ok=True)

    # ── resolve --version to an int version code ─────────────────────────
    resolved_vc: int | None = None
    if version is not None:
        if version.isdigit():
            try:
                resolved_vc = int(version)
            except ValueError:
                raise typer.BadParameter("Invalid version code.") from None
        else:
            try:
                resolved_vc, auth_data = _resolve_version_string(
                    package, version, arch, dispenser, country, proxy, profile
                )
            except VersionUnavailableError as exc:
                err.print(f"[red]{escape(str(exc))}[/red]")
                raise typer.Exit(code=VERSION_UNAVAILABLE_EXIT_CODE) from None
            except RateLimitedError as exc:
                _exit_transient_play_error(exc)
            except PlayAPIError as exc:
                err.print(f"[red]{escape(str(exc))}[/red]")
                raise typer.Exit(code=1) from None

    try:
        with console.status(
            f"Fetching delivery for [bold]{package}[/bold] ({arch})..."
        ):
            details, vc, delivery = _acquire_delivery(
                package,
                resolved_vc,
                arch,
                auth_data,
                dispenser,
                country,
                proxy,
                profile,
                _parse_locales(locale),
            )
    except RateLimitedError:
        err.print(
            "[red]Google Play rate limited delivery requests (HTTP 429); "
            "retry later.[/red]"
        )
        raise typer.Exit(code=RATE_LIMIT_EXIT_CODE) from None
    except VersionUnavailableError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=VERSION_UNAVAILABLE_EXIT_CODE) from None
    except PlayAPIError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None

    label = (
        f"requested version code {vc}"
        if version is not None and version.isdigit()
        else f"{escape(version or details.version_string)}  (vc {vc})"
    )
    rprint(Panel.fit(f"[bold]{escape(details.title)}[/bold]\n{label}", title=package))

    # ── build download specs ────────────────────────────────────────────
    base_name = f"{package}-{vc}.apk"
    base_path = output / base_name
    base_spec = DownloadSpec(
        url=delivery.gzipped_url or delivery.download_url,
        dest=base_path,
        cookies=delivery.cookies,
        label=base_name,
        gzipped=bool(delivery.gzipped_url),
        integrity_required=True,
        expected_size=delivery.download_size,
        sha1=delivery.sha1,
        sha256=delivery.sha256,
    )

    extras: list[DownloadSpec] = []
    if delivery.splits and not no_splits:
        for split in delivery.splits:
            split_dest = _safe_dest(output, f"{package}-{vc}-{split.name}.apk")
            extras.append(
                DownloadSpec(
                    url=split.gzipped_url or split.url,
                    dest=split_dest,
                    label=split_dest.name,
                    gzipped=bool(split.gzipped_url),
                    integrity_required=True,
                    expected_size=split.size,
                    sha1=split.sha1,
                    sha256=split.sha256,
                )
            )
    if dm and delivery.dex_metadata:
        name = f"{package}-{vc}.dm"
        extras.append(
            DownloadSpec(
                url=delivery.dex_metadata.url,
                dest=output / name,
                label=name,
                integrity_required=True,
                expected_size=delivery.dex_metadata.size,
                sha256=delivery.dex_metadata.sha256,
            )
        )
    if not no_extras and delivery.additional_files:
        for af in delivery.additional_files:
            if af.is_asset_pack:
                name = f"{package}-{vc}-{af.type_label}{af.extension}"
            else:
                name = f"{af.type_label}.{af.version_code}.{package}{af.extension}"
            extras.append(
                DownloadSpec(
                    url=af.url,
                    dest=output / name,
                    cookies=af.cookies,
                    label=name,
                    gzipped=af.gzipped,
                    expected_size=max(af.size, 0),
                )
            )

    all_specs = [base_spec] + extras
    _check_unique_dests(all_specs)
    total_files = len(all_specs)
    total_size = delivery.download_size + sum(
        s.size for s in delivery.splits if not no_splits
    )
    if dm and delivery.dex_metadata:
        total_size += delivery.dex_metadata.size
    if not no_extras:
        total_size += sum(af.size for af in delivery.additional_files)
    file_label = f"{total_files} file{'s' if total_files > 1 else ''}"
    rprint(f"\n[bold]Downloading {file_label}[/bold]  [dim]({_fmt(total_size)})[/dim]")
    try:
        download_batch(all_specs, require_integrity=require_integrity)
    except (IntegrityError, DownloadError) as exc:
        err.print(str(exc), style="red", markup=False)
        raise typer.Exit(code=1) from None

    # ── summary ──────────────────────────────────────────────────────────
    rprint()
    files_table = Table(title="Downloaded files", show_header=True)
    files_table.add_column("File", style="bold")
    files_table.add_column("Size", justify="right")
    files_table.add_row(base_name, _fmt(base_path.stat().st_size))

    if delivery.splits and not no_splits:
        for split in delivery.splits:
            sp = _safe_dest(output, f"{package}-{vc}-{split.name}.apk")
            if sp.exists():
                files_table.add_row(sp.name, _fmt(sp.stat().st_size))

    if dm and delivery.dex_metadata:
        dex_path = output / f"{package}-{vc}.dm"
        if dex_path.exists():
            files_table.add_row(dex_path.name, _fmt(dex_path.stat().st_size))

    if not no_extras and delivery.additional_files:
        for af in delivery.additional_files:
            if af.is_asset_pack:
                fname = f"{package}-{vc}-{af.type_label}{af.extension}"
            else:
                fname = f"{af.type_label}.{af.version_code}.{package}{af.extension}"
            ap = output / fname
            if ap.exists():
                files_table.add_row(ap.name, _fmt(ap.stat().st_size))

    console.print(files_table)

    if delivery.splits and not no_splits:
        command = _adb_install_command(all_specs)
        shell = " (PowerShell)" if os.name == "nt" else ""
        rprint(f"\n[dim]Tip: install split APKs to a device with{shell}[/dim]")
        console.print(
            f"  {command}", style="dim bold", markup=False, highlight=False, soft_wrap=True
        )

    rprint("\n[green bold]Download complete![/green bold]")
    return all_specs


# ── fast-download ────────────────────────────────────────────────────────────


@app.command("fast-download")
def fast_download(
    package: str = typer.Argument(
        ..., help="Package name (e.g. com.whatsapp).", callback=_package_callback
    ),
    output: Path = typer.Option(".", "--output", "-o", help="Output directory."),
    arch: str = typer.Option(
        "arm64",
        "--arch",
        "-a",
        help="Device type: arm64, armv7, x86, x86_64 or tv.",
        callback=_arch_callback,
    ),
    dispenser: str | None = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    no_splits: bool = typer.Option(
        True, "--no-splits/--splits", help="Include split APKs (default: skipped)."
    ),
    no_extras: bool = typer.Option(
        True,
        "--no-extras/--extras",
        help="Include OBB / asset packs (default: skipped).",
    ),
    country: str | None = typer.Option(
        None, "--country", "-c", help="2-letter country code (e.g. IN, US)."
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        "-p",
        help="Proxy for Google FDFE calls. Dispenser token fetches rotate proxy regions automatically.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Device profile key or name substring. Defaults to a random pick from the two newest profiles.",
    ),
) -> None:
    """Download the latest APK with a single reused/cached token — no multi-profile probing.

    Unlike download, this doesn't build a 5-token pool or probe multiple device
    profiles: it reuses a valid pooled token for --country if one exists, or
    fetches exactly one fresh token otherwise. Meant for high-volume/parallel
    callers (e.g. crawlers) where hammering the dispenser per-call gets IPs blocked.
    """
    try:
        path, version_string = download_app(
            package,
            output,
            arch=arch,
            country=country,
            proxy=proxy,
            dispenser_url=dispenser,
            profile=profile,
            no_splits=no_splits,
            no_extras=no_extras,
        )
    except (IntegrityError, DownloadError) as exc:
        err.print(str(exc), style="red", markup=False)
        raise typer.Exit(code=1) from None
    except PoolBusyError:
        _exit_pool_busy(1)
    except RateLimitedError as exc:
        if isinstance(exc.__cause__, DispenserRateLimitedError):
            _exit_token_error(exc.__cause__)
        err.print(
            "[red]Google Play rate limited delivery requests (HTTP 429); "
            "retry later.[/red]"
        )
        raise typer.Exit(code=RATE_LIMIT_EXIT_CODE) from None
    except PlayAPIError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None

    # soft_wrap: a long --output path must never get line-broken here — scripts
    # (e.g. the Airflow goopdl fallback) parse this exact line from stdout.
    console.print(
        f"[green bold]Downloaded[/green bold] {escape(str(path))}  "
        f"[dim]({escape(version_string)})[/dim]",
        soft_wrap=True,
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _exit_transient_play_error(exc: PlayAPIError) -> NoReturn:
    """Exit on Google Play throttling (code 5) or network/malformed reply (code 1)."""
    if isinstance(exc, RateLimitedError):
        err.print("[red]Google Play rate limited requests (HTTP 429); retry later.[/red]")
        raise typer.Exit(code=RATE_LIMIT_EXIT_CODE) from None
    err.print(f"[red]{escape(str(exc))}[/red]")
    raise typer.Exit(code=1) from None


def _exit_pool_busy(code: int) -> NoReturn:
    err.print(
        "[red]Token pool is busy (another goopdl process holds the lock); "
        "retry later.[/red]"
    )
    raise typer.Exit(code=code) from None


def _exit_token_error(exc: DirectAuthError) -> NoReturn:
    """Exit telling dispenser throttling (code 5) apart from an outage (code 3)."""
    if exc.status == 429:
        source = (
            "Token dispenser" if isinstance(exc, DispenserRateLimitedError)
            else "Direct Google authentication"
        )
        err.print(
            f"[red]{source} rate limited token requests (HTTP 429); "
            "retry later.[/red]"
        )
        raise typer.Exit(code=RATE_LIMIT_EXIT_CODE) from None
    if isinstance(exc, DispenserUnavailableError) and exc.reason == "profile_rejected":
        # The dispenser answered: it is up, it just refused every profile.
        last = "no authToken" if exc.status == 200 else f"HTTP {exc.status}"
        err.print(
            "[red]Authentication failed — all profiles rejected "
            f"(last dispenser response: {last}).[/red]"
        )
    elif isinstance(exc, DispenserUnavailableError):
        err.print(f"[red]{escape(str(exc))} It may be down; retry later.[/red]")
    else:
        err.print("[red]Direct Google authentication failed.[/red]")
    raise typer.Exit(code=AUTH_UNAVAILABLE_EXIT_CODE) from None


def _device_label(auth_data: dict) -> str:
    if auth_data.get("_device_profile"):
        return auth_data["_device_profile"]
    ua = auth_data.get("deviceInfoProvider", {}).get("userAgentString", "")
    m = re.search(r"model=([^,)]+)", ua)
    return m.group(1).replace("%20", " ") if m else "N/A"


def _require_auth(
    arch: str,
    dispenser: str | None,
    *,
    force: bool = False,
    proxy: str | None = None,
    country: str | None = None,
    profile: str | None = None,
) -> dict:
    """Return next round-robin pool token, ensuring pool is full first."""
    data = pick_pool_token(
        arch=arch,
        country=country,
        proxy=proxy,
        dispenser_url=dispenser,
        profile=profile,
    )
    if not data:
        err.print(
            "[red]Could not obtain an auth token. "
            "Try running [bold]goopdl auth token[/bold] first.[/red]"
        )
        raise typer.Exit(code=1) from None
    return data


def _fetch_probe_token(**kwargs) -> dict | None:
    """Stop profile/cohort probes on token endpoint throttling or outages.

    Direct auth 5xx/transport failures first get download_app's bounded
    cooldown retries; direct 400/401/403 (revoked credentials) do not.
    """
    attempt = 1
    while True:
        try:
            return fetch_token(**kwargs, raise_on_error=True)
        except DirectAuthError as exc:
            if exc.status == 429:
                raise RateLimitedError(str(exc), retry_after=exc.retry_after) from None
            if isinstance(exc, DispenserUnavailableError):
                if exc.reason == "profile_rejected":
                    return None  # another device profile/cohort may still get a token
                raise NetworkError(str(exc)) from None
            if exc.status in (400, 401, 403) or attempt >= _TOKEN_FETCH_ATTEMPTS:
                raise PlayAPIError(str(exc), status_code=exc.status) from None
        attempt += 1
        time.sleep(_TOKEN_FETCH_COOLDOWN)


def _resolve_version_string(
    package: str,
    version_str: str,
    arch: str,
    dispenser: str | None,
    country: str | None,
    proxy: str | None,
    profile: str | None,
) -> tuple[int, dict]:
    """Probe fresh tokens until one sees the requested version string.

    Returns (version_code, auth_data_for_that_cohort). The caller must use
    the returned auth for the subsequent purchase/delivery flow so the cohort
    stays consistent.
    Raises PlayAPIError if not found within 20 probes.
    """
    rprint(f"[dim]Resolving [bold]{escape(version_str)}[/bold] — probing fresh tokens...[/dim]")
    for attempt in range(1, 21):
        token = _fetch_probe_token(
            arch=arch,
            country=country,
            profile=profile,
            dispenser_url=dispenser,
            proxy=proxy,
        )
        if token is None:
            err.print(
                f"[yellow]  attempt {attempt}: no token for this profile, sleeping 5s...[/yellow]"
            )
            time.sleep(5)
            continue
        try:
            details = get_details(package, token, country=country, proxy=proxy)
        except (RateLimitedError, NetworkError, MalformedResponseError):
            raise  # more fresh tokens would only spend dispenser quota
        except PlayAPIError as exc:
            err.print(f"[dim]  attempt {attempt}: {escape(str(exc))}[/dim]")
            time.sleep(1)
            continue
        if details.version_string == version_str:
            rprint(
                f"[green]  Found {escape(version_str)} -> vc={details.version_code} (attempt {attempt})[/green]"
            )
            return details.version_code, token
        err.print(
            f"[dim]  attempt {attempt}: got {escape(details.version_string)}, "
            f"want {escape(version_str)}[/dim]"
        )
        time.sleep(1)
    raise VersionUnavailableError(
        f"Version string '{version_str}' not found in 20 probes — "
        "it may not be in active rollout for any fresh GSF ID."
    )


def _fmt(size_bytes: float) -> str:
    """Format bytes as a human-readable string."""
    if not size_bytes:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
