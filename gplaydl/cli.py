"""Typer CLI application — auth, download, info, search, list-splits."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gplaydl import __version__
from gplaydl.api import (
    AuthExpiredError,
    PlayAPIError,
    download_app,
    fetch_app_item,
    get_delivery,
    get_details,
    get_details_raw,
    parse_app_item,
    list_splits as api_list_splits,
    purchase,
    search_apps,
)
from gplaydl.auth import (
    clear_auth,
    DEFAULT_PROBES,
    ensure_auth,
    ensure_pool,
    fetch_token,
    pick_pool_token,
    replace_pool_token,
    save_auth,
)
from gplaydl.profiles import (
    ARM64_PROFILES,
    ARMV7_PROFILES,
    find_profile,
    get_latest_probe_profiles,
)
from gplaydl.download import DownloadSpec, download_batch

console = Console()
err = Console(stderr=True)

# Distinct exit code so callers can tell "Google Play is unreachable right now"
# from "this artifact cannot be trusted" without parsing human-readable output.
AUTH_UNAVAILABLE_EXIT_CODE = 3

app = typer.Typer(
    name="gplaydl",
    help="Download APKs from Google Play Store with anonymous authentication.",
    add_completion=False,
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        rprint(f"gplaydl [bold]{__version__}[/bold]")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(  # noqa: ARG001
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """GPlay APK Downloader — download APKs from Google Play Store."""


# ── auth ────────────────────────────────────────────────────────────────────


@app.command()
def auth(
    arch: str = typer.Option("arm64", help="Architecture: arm64 or armv7."),
    dispenser: Optional[str] = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove all cached tokens."),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code to register device in that region (e.g. IN, JP, BR).",
    ),
    proxy: Optional[str] = typer.Option(
        None, "--proxy", "-p", help="Proxy URL for dispenser + FDFE calls."
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Device profile key or name substring (e.g. 'Pv' or 'samsung'). Run 'gplaydl profiles' to list all.",
    ),
) -> None:
    """Acquire an anonymous auth token from the dispenser.

    Use --country to register the device with a specific region's MCC/MNC,
    so subsequent info/download calls with the same --country get that
    region's catalog without needing a proxy.
    """
    if clear:
        clear_auth()
        rprint("[green]All cached tokens removed.[/green]")
        raise typer.Exit()

    rprint(f"[dim]Dispenser:[/dim] {dispenser or 'https://auroraoss.com/api/auth'}")
    rprint(f"[dim]Architecture:[/dim] {arch}")
    if country:
        rprint(f"[dim]Country:[/dim] {country.upper()}")
    rprint()

    with console.status("Rotating through device profiles..."):
        data = fetch_token(
            dispenser_url=dispenser,
            arch=arch,
            country=country,
            proxy=proxy,
            profile=profile,
        )

    if not data:
        err.print("[red]Authentication failed — all profiles rejected.[/red]")
        raise typer.Exit(code=AUTH_UNAVAILABLE_EXIT_CODE)

    path = save_auth(data, arch, country)
    rprint(
        Panel.fit(
            f"[bold green]Authenticated[/bold green]\n"
            f"Email  : {data.get('email', 'N/A')}\n"
            f"GSF ID : {data.get('gsfId', 'N/A')}\n"
            f"Saved  : {path}",
            title="Token",
        )
    )


# ── profiles ────────────────────────────────────────────────────────────────


@app.command()
def profiles(
    arch: str = typer.Option("all", help="Filter by arch: arm64, armv7, or all."),
) -> None:
    """List available device profiles."""
    pool: list[tuple[str, dict]] = []
    if arch in ("arm64", "all"):
        pool += [(k, p, "arm64") for k, p in ARM64_PROFILES]
    if arch in ("armv7", "all"):
        pool += [(k, p, "armv7") for k, p in ARMV7_PROFILES]

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
    package: str = typer.Argument(..., help="Package name (e.g. com.whatsapp)."),
    arch: str = typer.Option("arm64", help="Architecture for token."),
    country: Optional[str] = typer.Option(
        None, "--country", "-c", help="2-letter country code."
    ),
    dispenser: Optional[str] = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    stable: int = typer.Option(
        3,
        "--stable",
        "-s",
        help="Stop early when max version unchanged for this many consecutive probes (default 3).",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Device profile for all probes (e.g. 'Galaxy S25 Ultra'). Defaults to top-ranked.",
    ),
    proxy: Optional[str] = typer.Option(
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
            raise typer.Exit(code=1)
        profile_key, profile_data = found
    else:
        top = get_latest_probe_profiles(arch, n=1)
        if not top:
            err.print("[red]No profiles available.[/red]")
            raise typer.Exit(code=1)
        profile_key, profile_data = top[0]

    device_name = profile_data.get("UserReadableName", profile_key)
    rprint(
        f"[dim]Profile:[/dim] {device_name}  [dim]Pool size:[/dim] {DEFAULT_PROBES}  [dim]Stable threshold:[/dim] {stable}"
    )

    # Ensure regional pool has DEFAULT_PROBES tokens — only hits dispenser for deficit
    tokens = ensure_pool(
        arch=arch,
        country=country,
        proxy=proxy,
        dispenser_url=dispenser,
        profile=profile_key,
    )
    if not tokens:
        err.print(
            "[red]Could not obtain any tokens — dispenser may be rate-limiting.[/red]"
        )
        raise typer.Exit(code=1)

    results: list[tuple[str, str, int]] = []
    best_vc = 0
    consecutive_stable = 0

    for i, token in enumerate(tokens):
        gsf_prefix = str(token.get("gsfId", "?"))[:8]
        try:
            details = get_details(package, token, country=country, proxy=proxy)
        except AuthExpiredError:
            err.print(
                f"[dim]  probe {i+1}: {gsf_prefix}... token expired — replacing[/dim]"
            )
            new_token = replace_pool_token(
                token,
                arch=arch,
                country=country,
                proxy=proxy,
                dispenser_url=dispenser,
                profile=profile_key,
            )
            if new_token is None:
                err.print(
                    f"[dim]  probe {i+1}: replacement unavailable, skipping[/dim]"
                )
                continue
            tokens[i] = new_token
            try:
                details = get_details(package, new_token, country=country, proxy=proxy)
            except PlayAPIError as exc:
                err.print(f"[dim]  probe {i+1}: retry failed: {exc}[/dim]")
                continue
        except PlayAPIError as exc:
            err.print(f"[dim]  probe {i+1}: {gsf_prefix}... error: {exc}[/dim]")
            continue

        vc = details.version_code
        vs = details.version_string
        results.append((gsf_prefix, vs, vc))

        if vc > best_vc:
            best_vc = vc
            consecutive_stable = 0
            rprint(
                f"  probe {i+1}/{len(tokens)}: gsf={gsf_prefix}... {vs} ({vc}) [bold green]↑ new max[/bold green]"
            )
        else:
            consecutive_stable += 1
            rprint(
                f"  probe {i+1}/{len(tokens)}: gsf={gsf_prefix}... {vs} ({vc}) [dim](stable {consecutive_stable}/{stable})[/dim]"
            )

        if consecutive_stable >= stable:
            rprint(f"[dim]  Converged after {i+1} probes.[/dim]")
            break

    if not results:
        err.print("[red]Could not fetch version from any probe.[/red]")
        raise typer.Exit(code=1)

    results.sort(key=lambda x: x[2], reverse=True)
    _, best_version, best_vc_final = results[0]

    table = Table(title=f"Probe Results: {package}", show_header=True)
    table.add_column("GSF prefix", style="dim")
    table.add_column("Version")
    table.add_column("Version Code", justify="right")
    for gsf, vs, vc in results:
        style = "bold green" if vc == best_vc_final else ""
        table.add_row(gsf + "...", vs, str(vc), style=style)
    console.print(table)

    rprint(
        f"\n[bold green]Latest:[/bold green] {best_version} ({best_vc_final})"
        + (f"  [dim]Region: {country.upper()}[/dim]" if country else "")
    )


# ── info ────────────────────────────────────────────────────────────────────


@app.command()
def info(
    package: str = typer.Argument(..., help="Package name (e.g. com.whatsapp)."),
    arch: str = typer.Option("arm64", help="Architecture for token."),
    dispenser: Optional[str] = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code (e.g. US, IN, DE). Sets gl= and locale headers. For true regional versions, combine with --proxy.",
    ),
    proxy: Optional[str] = typer.Option(
        None,
        "--proxy",
        "-p",
        help="Proxy URL for FDFE calls, e.g. socks5://host:port or http://host:port. Routes requests through a regional IP.",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Device profile key or name substring (e.g. 'D2' or 'samsung'). Run 'gplaydl profiles' to list all.",
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
                    replace_pool_token(
                        auth_data,
                        arch=arch,
                        country=country,
                        proxy=proxy,
                        dispenser_url=dispenser,
                    )
                    auth_data = _require_auth(
                        arch, dispenser, country=country, proxy=proxy, profile=profile
                    )
                    details = get_details(
                        package, auth_data, country=country, proxy=proxy
                    )
        except PlayAPIError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    if raw:
        print(_json.dumps(item, indent=2, ensure_ascii=False))
        return

    table = Table(title=details.title or package, show_header=False, title_style="bold")
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Package", details.package)
    table.add_row("Version", f"{details.version_string} ({details.version_code})")
    if country:
        table.add_row("Region", country.upper())
    table.add_row("Device", _device_label(auth_data))
    table.add_row("Developer", details.developer or "N/A")
    table.add_row("Rating", details.rating or "N/A")
    table.add_row("Downloads", details.downloads or "N/A")
    table.add_row("Play Store", details.play_url)
    console.print(table)


# ── search ──────────────────────────────────────────────────────────────────


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results."),
    arch: str = typer.Option("arm64", help="Architecture for token."),
    dispenser: Optional[str] = typer.Option(
        None, "--dispenser", "-d", help="Custom dispenser URL."
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code for regional search results.",
    ),
    proxy: Optional[str] = typer.Option(
        None, "--proxy", "-p", help="Proxy URL for FDFE calls."
    ),
    profile: Optional[str] = typer.Option(
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
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    if not results:
        rprint("[yellow]No results found.[/yellow]")
        raise typer.Exit()

    table = Table(title=f'Results for "{query}"')
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("Package")
    for i, app_item in enumerate(results, 1):
        table.add_row(str(i), app_item["title"], app_item["package"])
    console.print(table)


# ── list-splits ─────────────────────────────────────────────────────────────


@app.command("list-splits")
def list_splits_cmd(
    package: str = typer.Argument(..., help="Package name."),
    arch: str = typer.Option("arm64", help="Architecture for token."),
    dispenser: Optional[str] = typer.Option(
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
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    if not splits:
        rprint(f"[yellow]{package} has no split APKs.[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Splits for {package}")
    table.add_column("#", style="dim", width=4)
    table.add_column("Split name")
    for i, name in enumerate(splits, 1):
        table.add_row(str(i), name)
    console.print(table)
    rprint(f"\n[dim]Total: {len(splits)} splits[/dim]")


# ── download ────────────────────────────────────────────────────────────────


@app.command()
def download(
    package: str = typer.Argument(..., help="Package name (e.g. com.whatsapp)."),
    output: Path = typer.Option(".", "--output", "-o", help="Output directory."),
    arch: str = typer.Option(
        "arm64", "--arch", "-a", help="Architecture: arm64 or armv7."
    ),
    version: Optional[str] = typer.Option(
        None,
        "--version",
        "-v",
        help="Version code (e.g. 384009971) or version string (e.g. 434.0.0.44.74).",
    ),
    dispenser: Optional[str] = typer.Option(
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
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="2-letter country code (e.g. IN, US). Use with --proxy for true regional APK variants.",
    ),
    proxy: Optional[str] = typer.Option(
        None, "--proxy", "-p", help="Proxy URL for FDFE calls, e.g. socks5://host:port."
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile", help="Device profile key or name substring."
    ),
    integrity_manifest: Optional[Path] = typer.Option(
        None,
        "--integrity-manifest",
        help="Write verified file metadata as JSON after all downloads pass.",
    ),
) -> None:
    """Download an APK (with splits + additional files) from Google Play."""
    output.mkdir(parents=True, exist_ok=True)
    auth_data = pick_pool_token(
        arch=arch,
        country=country,
        proxy=proxy,
        dispenser_url=dispenser,
        profile=profile,
    )
    if not auth_data:
        err.print(
            "[red]Could not obtain an auth token — dispenser may be rate-limiting.[/red]"
        )
        raise typer.Exit(code=AUTH_UNAVAILABLE_EXIT_CODE)

    # ── resolve --version to an int version code ─────────────────────────
    resolved_vc: Optional[int] = None
    if version is not None:
        if version.isdigit():
            resolved_vc = int(version)
        else:
            try:
                resolved_vc, auth_data = _resolve_version_string(
                    package, version, arch, dispenser, country, proxy, profile
                )
            except PlayAPIError as exc:
                err.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1)

    # ── details + purchase + delivery (with auto-retry on expired token) ─
    try:
        try:
            with console.status(f"Fetching details for [bold]{package}[/bold]..."):
                details = get_details(package, auth_data, country=country, proxy=proxy)
            vc = resolved_vc if resolved_vc is not None else details.version_code
            with console.status("Acquiring app and fetching download URLs..."):
                purchase(package, vc, auth_data, country=country, proxy=proxy)
                delivery = get_delivery(
                    package, vc, auth_data, country=country, proxy=proxy
                )
        except AuthExpiredError:
            new_token = replace_pool_token(
                auth_data,
                arch=arch,
                country=country,
                proxy=proxy,
                dispenser_url=dispenser,
                profile=profile,
            )
            if not new_token:
                err.print("[red]Token expired and replacement failed.[/red]")
                raise typer.Exit(code=AUTH_UNAVAILABLE_EXIT_CODE)
            auth_data = new_token
            with console.status(f"Fetching details for [bold]{package}[/bold]..."):
                details = get_details(package, auth_data, country=country, proxy=proxy)
            vc = resolved_vc if resolved_vc is not None else details.version_code
            with console.status("Acquiring app and fetching download URLs..."):
                purchase(package, vc, auth_data, country=country, proxy=proxy)
                delivery = get_delivery(
                    package, vc, auth_data, country=country, proxy=proxy
                )
    except PlayAPIError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    rprint(
        Panel.fit(
            f"[bold]{details.title}[/bold]\n" f"{details.version_string}  (vc {vc})",
            title=package,
        )
    )

    # ── build download specs ────────────────────────────────────────────
    base_name = f"{package}-{vc}.apk"
    base_path = output / base_name
    base_spec = DownloadSpec(
        url=delivery.download_url,
        dest=base_path,
        cookies=delivery.cookies,
        label=base_name,
        integrity_required=True,
        expected_size=delivery.download_size,
        sha1=delivery.sha1,
        sha256=delivery.sha256,
    )

    extras: list[DownloadSpec] = []
    if delivery.splits and not no_splits:
        for split in delivery.splits:
            name = f"{package}-{vc}-{split.name}.apk"
            extras.append(
                DownloadSpec(
                    url=split.url,
                    dest=output / name,
                    label=name,
                    integrity_required=True,
                    expected_size=split.size,
                    sha1=split.sha1,
                    sha256=split.sha256,
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
                )
            )

    all_specs = [base_spec] + extras
    total_files = len(all_specs)
    total_size = delivery.download_size + sum(
        s.size for s in delivery.splits if not no_splits
    )
    if not no_extras:
        total_size += sum(af.size for af in delivery.additional_files)
    file_label = f"{total_files} file{'s' if total_files > 1 else ''}"
    rprint(f"\n[bold]Downloading {file_label}[/bold]  [dim]({_fmt(total_size)})[/dim]")
    download_batch(all_specs, integrity_manifest=integrity_manifest)

    # ── summary ──────────────────────────────────────────────────────────
    rprint()
    files_table = Table(title="Downloaded files", show_header=True)
    files_table.add_column("File", style="bold")
    files_table.add_column("Size", justify="right")
    files_table.add_row(base_name, _fmt(base_path.stat().st_size))

    if delivery.splits and not no_splits:
        for split in delivery.splits:
            sp = output / f"{package}-{vc}-{split.name}.apk"
            if sp.exists():
                files_table.add_row(sp.name, _fmt(sp.stat().st_size))

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
        rprint(
            "\n[dim]Tip: install split APKs to a device with "
            "[bold]adb install-multiple *.apk[/bold][/dim]"
        )

    rprint("\n[green bold]Download complete![/green bold]")


# ── fast-download ────────────────────────────────────────────────────────────


@app.command("fast-download")
def fast_download(
    package: str = typer.Argument(..., help="Package name (e.g. com.whatsapp)."),
    output: Path = typer.Option(".", "--output", "-o", help="Output directory."),
    arch: str = typer.Option(
        "arm64", "--arch", "-a", help="Architecture: arm64 or armv7."
    ),
    dispenser: Optional[str] = typer.Option(
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
    country: Optional[str] = typer.Option(
        None, "--country", "-c", help="2-letter country code (e.g. IN, US)."
    ),
    proxy: Optional[str] = typer.Option(
        None,
        "--proxy",
        "-p",
        help="Proxy for Google FDFE calls. Dispenser token fetches rotate proxy regions automatically.",
    ),
    profile: Optional[str] = typer.Option(
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
    except PlayAPIError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    # soft_wrap: a long --output path must never get line-broken here — scripts
    # (e.g. the Airflow gplaydl fallback) parse this exact line from stdout.
    console.print(
        f"[green bold]Downloaded[/green bold] {path}  [dim]({version_string})[/dim]",
        soft_wrap=True,
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _device_label(auth_data: dict) -> str:
    if auth_data.get("_device_profile"):
        return auth_data["_device_profile"]
    ua = auth_data.get("deviceInfoProvider", {}).get("userAgentString", "")
    m = re.search(r"model=([^,)]+)", ua)
    return m.group(1).replace("%20", " ") if m else "N/A"


def _require_auth(
    arch: str,
    dispenser: Optional[str],
    *,
    force: bool = False,
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
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
            "Try running [bold]gplaydl auth[/bold] first.[/red]"
        )
        raise typer.Exit(code=1)
    return data


def _resolve_version_string(
    package: str,
    version_str: str,
    arch: str,
    dispenser: Optional[str],
    country: Optional[str],
    proxy: Optional[str],
    profile: Optional[str],
) -> tuple[int, dict]:
    """Probe fresh tokens until one sees the requested version string.

    Returns (version_code, auth_data_for_that_cohort). The caller must use
    the returned auth for the subsequent purchase/delivery flow so the cohort
    stays consistent.
    Raises PlayAPIError if not found within 20 probes.
    """
    rprint(f"[dim]Resolving [bold]{version_str}[/bold] — probing fresh tokens...[/dim]")
    for attempt in range(1, 21):
        token = fetch_token(
            arch=arch, profile=profile, dispenser_url=dispenser, proxy=proxy
        )
        if token is None:
            err.print(
                f"[yellow]  attempt {attempt}: rate-limited, sleeping 5s...[/yellow]"
            )
            time.sleep(5)
            continue
        try:
            details = get_details(package, token, country=country, proxy=proxy)
        except PlayAPIError as exc:
            err.print(f"[dim]  attempt {attempt}: {exc}[/dim]")
            time.sleep(1)
            continue
        if details.version_string == version_str:
            rprint(
                f"[green]  Found {version_str} → vc={details.version_code} (attempt {attempt})[/green]"
            )
            return details.version_code, token
        err.print(
            f"[dim]  attempt {attempt}: got {details.version_string}, want {version_str}[/dim]"
        )
        time.sleep(1)
    raise PlayAPIError(
        f"Version string '{version_str}' not found in 20 probes — "
        "it may not be in active rollout for any fresh GSF ID."
    )


def _fmt(size_bytes: int | float) -> str:
    """Format bytes as a human-readable string."""
    if not size_bytes:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
