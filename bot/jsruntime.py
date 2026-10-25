"""Locate a JavaScript runtime for yt-dlp's YouTube challenges.

yt-dlp (2025+) solves YouTube's signature and `n` challenges by running
JavaScript in an external runtime — deno, node, quickjs or bun — together with
the solver scripts shipped by the `yt-dlp-ejs` pip package. Without both, a
cookie-authenticated (web client) extraction silently loses most formats, which
surfaces as:

    ERROR: [youtube] <id>: Requested format is not available.
    WARNING: [youtube] <id>: Signature solving failed ... supported JavaScript runtime

We ship deno through the `deno` PyPI package (a real manylinux/win binary), so
this module only has to find it. Search order:

  1. YTDLP_JS_RUNTIME env var — "deno" or "deno:/abs/path/to/deno"
  2. deno on PATH
  3. deno installed beside the running interpreter (venv Scripts/bin)
  4. deno shipped inside the `deno` package directory
  5. the app's own pip --prefix location (<app>/.local/bin/deno, what the
     hosting panel produces)
  6. node / bun / qjs on PATH as fallbacks

Returns (name, path_or_None) or None. It never raises and never downloads
anything: a missing runtime degrades gracefully instead of breaking startup.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

import logging

log = logging.getLogger("bot.jsruntime")

__all__ = ["detect", "describe"]

_EXE = ".exe" if os.name == "nt" else ""
_ALLOWED = ("deno", "node", "quickjs", "bun")
_APP_DIR = Path(__file__).resolve().parent.parent


def _interpreters_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _candidate_paths(name: str) -> list[Path]:
    """Likely on-disk locations for a runtime binary."""
    out: list[Path] = []
    for base in (_interpreters_dir(), _APP_DIR / ".local" / "bin", Path(sys.prefix) / "bin"):
        out.append(base / f"{name}{_EXE}")
    # the deno PyPI package keeps the binary inside its own directory
    try:
        import deno as deno_pkg  # type: ignore

        pkg_dir = Path(deno_pkg.__file__).resolve().parent
        out.append(pkg_dir / f"{name}{_EXE}")
        out.extend(sorted(pkg_dir.rglob(f"{name}{_EXE}"))[:2])
    except Exception:  # noqa: BLE001 - package is optional at runtime
        pass
    return out


def detect() -> Optional[Tuple[str, Optional[str]]]:
    """Return (runtime_name, path_or_None) for a usable JS runtime."""
    override = (os.environ.get("YTDLP_JS_RUNTIME") or "").strip()
    if override:
        name, _, path = override.partition(":")
        name = name.strip().lower()
        if name in _ALLOWED:
            if path and Path(path).exists():
                return name, path
            return name, None

    for name in _ALLOWED:
        found = shutil.which(name)
        if found:
            return name, None
        for cand in _candidate_paths(name):
            if cand.exists():
                return name, str(cand)
    return None


def describe() -> str:
    runtime = detect()
    if not runtime:
        return "none (YouTube formats will be limited)"
    name, path = runtime
    return f"{name}{' at ' + path if path else ' (from PATH)'}"


def log_once() -> Optional[Tuple[str, Optional[str]]]:
    """Detect and log the runtime in use, exactly once per process."""
    runtime = detect()
    if getattr(log_once, "_done", False):
        return runtime
    log_once._done = True  # type: ignore[attr-defined]
    try:
        import yt_dlp_ejs  # type: ignore

        ejs = f"yt-dlp-ejs {getattr(yt_dlp_ejs, 'version', '?')}"
    except Exception:  # noqa: BLE001
        ejs = "yt-dlp-ejs MISSING (pip install yt-dlp-ejs)"
    if runtime:
        name, path = runtime
        log.info("youtube challenge runtime: %s (%s; %s)", name, path or "PATH", ejs)
    else:
        log.warning(
            "no JavaScript runtime found for YouTube challenges (%s) — "
            "cookie-authenticated formats will be limited", ejs,
        )
    return runtime
