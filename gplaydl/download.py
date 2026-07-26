"""File download with httpx (async) and Rich progress bars."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

CHUNK_SIZE = 64 * 1024  # 64 KB
MAX_CONCURRENT = 4
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST_LENGTHS = {"sha1": 20, "sha256": 32}


class IntegrityError(RuntimeError):
    """Raised when delivery metadata or downloaded bytes fail verification."""


def make_progress() -> Progress:
    """Create a pre-configured Rich progress bar for downloads."""
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
        raise IntegrityError(f"missing integrity metadata for {spec.label or spec.dest.name}")
    return algorithm, encoded, _decode_digest(encoded, algorithm)


async def _download_one(
    spec: DownloadSpec,
    client: httpx.AsyncClient,
    progress: Progress,
    sem: asyncio.Semaphore,
) -> Path:
    """Stream-download one file, verify final bytes, then publish it atomically."""
    async with sem:
        headers: dict[str, str] = {}
        if spec.cookies:
            parts = [f"{c['name']}={c['value']}" for c in spec.cookies]
            headers["Cookie"] = "; ".join(parts)

        label = spec.label or spec.dest.name
        task_id = progress.add_task("download", filename=label, total=None)
        integrity = (
            _select_digest(spec)
            if (spec.integrity_required or spec.expected_size or spec.sha1 or spec.sha256)
            else None
        )
        calculated = hashlib.new(integrity[0]) if integrity else None
        written = 0
        temporary = spec.dest.with_name(spec.dest.name + ".part")
        temporary.unlink(missing_ok=True)

        try:
            decompressor = (
                zlib.decompressobj(zlib.MAX_WBITS | 16) if spec.gzipped else None
            )
            async with client.stream("GET", spec.url, headers=headers) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                if total:
                    progress.update(task_id, total=total)

                with temporary.open("wb") as downloaded:
                    async for chunk in resp.aiter_bytes(chunk_size=CHUNK_SIZE):
                        output = decompressor.decompress(chunk) if decompressor else chunk
                        downloaded.write(output)
                        written += len(output)
                        if calculated:
                            calculated.update(output)
                        progress.advance(task_id, len(chunk))

                    if decompressor:
                        output = decompressor.flush()
                        downloaded.write(output)
                        written += len(output)
                        if calculated:
                            calculated.update(output)

            if integrity:
                _, _, expected = integrity
                if written != spec.expected_size:
                    raise IntegrityError(f"size mismatch for {label}")
                if not hmac.compare_digest(calculated.digest(), expected):
                    raise IntegrityError(f"digest mismatch for {label}")
            os.replace(temporary, spec.dest)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    return spec.dest


async def _run_downloads(specs: list[DownloadSpec]) -> None:
    """Download all files in parallel (up to MAX_CONCURRENT at once)."""
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)
    progress = make_progress()

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        with progress:
            await asyncio.gather(*[_download_one(s, client, progress, sem) for s in specs])


def _write_manifest(path: Path, specs: list[DownloadSpec]) -> None:
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
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps({"version": 1, "files": files}, sort_keys=True), encoding="utf-8"
    )
    os.replace(temporary, path)


def download_batch(
    specs: list[DownloadSpec], integrity_manifest: Optional[Path] = None
) -> None:
    """Download files and optionally write manifest after every file verifies."""
    if integrity_manifest:
        for spec in specs:
            _select_digest(spec)
    asyncio.run(_run_downloads(specs))
    if integrity_manifest:
        _write_manifest(integrity_manifest, specs)
