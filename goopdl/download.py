"""File download with httpx (async); Rich progress bars only on the sync/CLI path."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from goopdl.errors import GoopdlError

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID

# (label, bytes_done, total_bytes_or_None), called once at start and per chunk.
ProgressCallback = Callable[[str, int, "int | None"], None]

CHUNK_SIZE = 64 * 1024  # 64 KB
MAX_CONCURRENT = 4
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST_LENGTHS = {"sha1": 20, "sha256": 32}


class IntegrityError(GoopdlError, RuntimeError):
    """Raised when delivery metadata or downloaded bytes fail verification."""


class DownloadError(IntegrityError):
    """HTTP/network failure fetching a file from the CDN.

    Subclasses IntegrityError so existing ``except IntegrityError`` handlers
    keep catching CDN failures. To tell them apart, catch DownloadError first.

    ``status_code`` is the HTTP status (None for transport errors/timeouts).
    ``retryable`` is True when retrying the same URL later may succeed (429, 408,
    5xx, timeouts, dropped connections); a 403 usually means the signed URL expired and the delivery
    must be requested again. The message never contains the signed URL.
    """

    def __init__(
        self, message: str, status_code: int | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


# Transient transport failures only; UnsupportedProtocol, ProxyError and
# LocalProtocolError are configuration/client bugs that a retry won't fix.
_TRANSIENT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def _download_error(label: str, exc: httpx.HTTPError) -> DownloadError:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return DownloadError(
            f"download failed for {label} (HTTP {status})",
            status_code=status,
            retryable=status in (408, 429) or status >= 500,
        )
    return DownloadError(
        f"download failed for {label} ({type(exc).__name__})",
        retryable=isinstance(exc, _TRANSIENT_ERRORS),
    )


def make_progress() -> Progress:
    """Create a pre-configured Rich progress bar for downloads."""
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[filename]}"),
        BarColumn(bar_width=30),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )


@dataclass
class DownloadSpec:
    """Everything needed to download a single file."""

    url: str
    dest: Path
    cookies: list[dict] = field(default_factory=list)
    label: str = ""
    gzipped: bool = False
    integrity_required: bool = False
    expected_size: int = 0
    sha1: str = ""
    sha256: str = ""


def _decode_digest(value: str, algorithm: str) -> bytes:
    if not _BASE64URL_RE.fullmatch(value):
        raise IntegrityError(f"invalid {algorithm} Base64url digest")
    try:
        digest = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as error:
        raise IntegrityError(f"invalid {algorithm} Base64url digest") from error
    if len(digest) != _DIGEST_LENGTHS[algorithm]:
        raise IntegrityError(f"invalid {algorithm} digest length")
    if base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=") != value:
        raise IntegrityError(f"non-canonical {algorithm} Base64url digest")
    return digest


def _select_digest(spec: DownloadSpec) -> tuple[str, str, bytes]:
    algorithm, encoded = ("sha256", spec.sha256) if spec.sha256 else ("sha1", spec.sha1)
    if spec.expected_size <= 0 or not encoded:
        raise IntegrityError(
            f"missing integrity metadata for {spec.label or spec.dest.name}"
        )
    return algorithm, encoded, _decode_digest(encoded, algorithm)


def _file_matches(
    path: Path, expected_size: int, algorithm: str, digest: bytes
) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    calculated = hashlib.new(algorithm)
    with path.open("rb") as existing:
        while chunk := existing.read(1024 * 1024):
            calculated.update(chunk)
    return hmac.compare_digest(calculated.digest(), digest)


def _temporary_path(dest: Path) -> Path:
    # Unique per call: concurrent writers of the same dest must not share
    # (or delete) each other's partial file.
    return dest.with_name(f"{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part")


async def _download_one(
    spec: DownloadSpec,
    client: httpx.AsyncClient,
    on_progress: ProgressCallback | None,
    sem: asyncio.Semaphore,
) -> Path:
    """Stream-download one file, verify final bytes, then publish it atomically."""
    async with sem:
        headers: dict[str, str] = {}
        if spec.cookies:
            parts = [f"{c['name']}={c['value']}" for c in spec.cookies]
            headers["Cookie"] = "; ".join(parts)

        label = spec.label or spec.dest.name
        integrity = (
            _select_digest(spec)
            if (
                spec.integrity_required
                or spec.expected_size
                or spec.sha1
                or spec.sha256
            )
            else None
        )
        if integrity and await asyncio.to_thread(
            _file_matches, spec.dest, spec.expected_size, integrity[0], integrity[2]
        ):
            if on_progress:
                on_progress(label, spec.expected_size, spec.expected_size)
            return spec.dest

        calculated = hashlib.new(integrity[0]) if integrity else None
        written = 0
        temporary = _temporary_path(spec.dest)

        try:
            decompressor = (
                zlib.decompressobj(zlib.MAX_WBITS | 16) if spec.gzipped else None
            )
            async with client.stream("GET", spec.url, headers=headers) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0)) or None
                received = 0
                if on_progress:
                    on_progress(label, received, total)

                with temporary.open("wb") as downloaded:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        output = (
                            decompressor.decompress(chunk) if decompressor else chunk
                        )
                        downloaded.write(output)
                        written += len(output)
                        if calculated:
                            calculated.update(output)
                        received += len(chunk)
                        if on_progress:
                            on_progress(label, received, total)

                    if decompressor:
                        output = decompressor.flush()
                        downloaded.write(output)
                        written += len(output)
                        if calculated:
                            calculated.update(output)

            if integrity:
                _, _, expected = integrity
                assert calculated is not None
                if written != spec.expected_size:
                    raise IntegrityError(f"size mismatch for {label}")
                if not hmac.compare_digest(calculated.digest(), expected):
                    raise IntegrityError(f"digest mismatch for {label}")
            os.replace(temporary, spec.dest)
        except httpx.HTTPError as exc:
            # from None: the httpx error (and its traceback) carries the signed URL.
            raise _download_error(label, exc) from None
        except zlib.error as exc:
            raise DownloadError(
                f"corrupt gzip data for {label}", retryable=False
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    return spec.dest


async def _run_downloads(
    specs: list[DownloadSpec],
    on_progress: ProgressCallback | None = None,
    max_concurrent: int = MAX_CONCURRENT,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Download all files in parallel (up to max_concurrent at once)."""
    if client is None:
        timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as own:
            await _run_downloads(specs, on_progress, max_concurrent, own)
        return
    sem = asyncio.Semaphore(max_concurrent)
    tasks = [
        asyncio.create_task(_download_one(s, client, on_progress, sem)) for s in specs
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        # On the first failure (or if we are cancelled) stop every sibling and
        # wait for it, so its .part is removed and nothing lands on disk later.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _check_required_integrity(specs: list[DownloadSpec]) -> None:
    for spec in specs:
        if spec.integrity_required:
            _select_digest(spec)


async def download_files(
    specs: list[DownloadSpec],
    *,
    require_integrity: bool = False,
    on_progress: ProgressCallback | None = None,
    max_concurrent: int = MAX_CONCURRENT,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Async download for library callers; writes nothing to stdout.

    Returns None once every file is at its ``spec.dest``.

    Raises IntegrityError (missing metadata, size/digest mismatch) or
    DownloadError (HTTP/network failure). With require_integrity, refuses to
    start unless every integrity-required file (APKs, .dm) has digests.
    ``client`` is used as-is and not closed; by default a fresh one is created.

    ``on_progress`` runs inline in the download loop: if it raises, that
    download is aborted, the other downloads are cancelled and the callback's
    exception propagates unchanged. Raises ValueError if max_concurrent < 1.
    On failure or cancellation no partial (.part) files are left behind.
    """
    if max_concurrent < 1:
        raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
    if require_integrity:
        _check_required_integrity(specs)
    await _run_downloads(specs, on_progress, max_concurrent, client)


def _rich_callback(progress: Progress) -> ProgressCallback:
    """Adapt download_files progress events to Rich progress bars (CLI only)."""
    tasks: dict[str, TaskID] = {}

    def on_progress(label: str, done: int, total: int | None) -> None:
        if label in tasks:
            progress.update(tasks[label], total=total, completed=done)
            return
        # A first event that is already complete means the file was verified on disk.
        skipped = bool(done) and done == total
        tasks[label] = progress.add_task(
            "download",
            filename=f"{label} [dim](already downloaded)[/dim]" if skipped else label,
            total=total,
            completed=done,
        )

    return on_progress


def write_manifest(path: Path, specs: list[DownloadSpec]) -> None:
    files = []
    for spec in specs:
        algorithm, encoded, _ = _select_digest(spec)
        if spec.sha1:
            _decode_digest(spec.sha1, "sha1")
        if spec.sha256:
            _decode_digest(spec.sha256, "sha256")
        files.append(
            {
                "path": spec.dest.name,
                "size": spec.expected_size,
                "algorithm": algorithm,
                "digest": encoded,
                "google_sha1": spec.sha1,
                "google_sha256": spec.sha256,
            }
        )
    temporary = _temporary_path(path)
    try:
        temporary.write_text(
            json.dumps({"version": 1, "files": files}, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def download_batch(specs: list[DownloadSpec], require_integrity: bool = False) -> None:
    """Download files; with require_integrity, refuse to start unless every
    integrity-required file (APKs, .dm) has digests. OBB/asset packs never do.

    Synchronous wrapper with Rich progress bars; from async code await
    download_files() instead."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "download_batch() cannot run inside a running event loop; "
            "use 'await goopdl.download.download_files(...)' instead."
        )
    if require_integrity:
        _check_required_integrity(specs)
    with make_progress() as progress:
        asyncio.run(download_files(specs, on_progress=_rich_callback(progress)))
