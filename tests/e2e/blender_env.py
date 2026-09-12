"""Blender discovery + invocation helpers for the e2e gate evidence (G9).

Pure Python (no ``bpy``): this module is imported by the *host* pytest process,
never inside Blender. It answers two questions the acceptance gates ask:

- **G9 (Blender 경로 탐지)** — where is Blender on this machine? ``find_blender()``
  resolves an explicit ``BLENDER`` env var first, then ``PATH``, then the
  versioned Windows install layout (newest version wins), then the common
  macOS/Linux paths. Tests that need real Blender evidence are gated with
  ``requires_blender`` so a machine without Blender skips instead of failing.
- **G0 (재현 가능한 기준선)** — which Blender produced the evidence?
  ``blender_version_info()`` records the version string, build hash and build
  date reported by ``blender --version`` so a manifest can name its environment.

``run_blender_python()`` is the single place that spells the background-script
invocation (``blender --background --python <script> -- <args>``).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

import pytest

# Non-Windows install locations that are worth probing directly.
_COMMON_BLENDER = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    "/snap/bin/blender",
]

# Windows installs live under a per-version directory, e.g.
# ``C:/Program Files/Blender Foundation/Blender 5.1/blender.exe``.
_WINDOWS_GLOBS = [
    "C:/Program Files/Blender Foundation/Blender */blender.exe",
    "C:/Program Files (x86)/Blender Foundation/Blender */blender.exe",
]

_VERSION_DIR_RE = re.compile(r"Blender\s+([0-9]+(?:\.[0-9]+)*)\s*$")


def _version_sort_key(path: str) -> tuple:
    """Sort key for a versioned Windows install dir (newest last)."""
    parent = os.path.basename(os.path.dirname(path))
    m = _VERSION_DIR_RE.match(parent.strip())
    if not m:
        return ((), parent)
    return (tuple(int(p) for p in m.group(1).split(".")), parent)


def _windows_candidates() -> list[str]:
    found: list[str] = []
    for pattern in _WINDOWS_GLOBS:
        found.extend(p for p in glob.glob(pattern) if os.path.isfile(p))
    # Newest version first.
    found.sort(key=_version_sort_key, reverse=True)
    return found


def find_blender() -> str | None:
    """Locate a Blender executable, or ``None`` when none is installed (G9).

    Priority: ``$BLENDER`` (when it exists) > ``PATH`` > versioned Windows
    installs (highest version) > common macOS/Linux locations.
    """
    env = os.environ.get("BLENDER")
    if env and os.path.exists(env):
        return env
    found = shutil.which("blender")
    if found:
        return found
    if sys.platform.startswith("win"):
        for cand in _windows_candidates():
            return cand
    for p in _COMMON_BLENDER:
        if os.path.exists(p):
            return p
    # A Windows layout can exist on a non-Windows-reported platform (msys, wine);
    # probe it last so the normal paths still win.
    for cand in _windows_candidates():
        return cand
    return None


BLENDER = find_blender()

requires_blender = pytest.mark.skipif(
    BLENDER is None, reason="Blender not installed (G9 evidence requires a real Blender)"
)


def run_blender_python(
    script_path: str,
    args: list[str],
    *,
    timeout: int = 1800,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``script_path`` inside Blender in background mode.

    Everything after ``--`` is handed to the script (Blender ignores it), which is
    the convention every worker/fixture script in this repo parses.
    """
    if BLENDER is None:  # pragma: no cover - guarded by requires_blender
        raise RuntimeError("no Blender executable found")
    cmd = [BLENDER, "--background", "--python", script_path, "--", *args]
    # Blender localizes its console output; decode as UTF-8 with replacement so a
    # non-UTF-8 host locale (e.g. cp949 on Windows) cannot crash the reader thread.
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=cwd)


def blender_version_info(blender_path: str | None = None) -> dict:
    """Parse ``blender --version`` into ``{version, build_hash, build_date, raw}`` (G0).

    Missing fields come back as ``None`` rather than raising: the manifest records
    whatever this Blender build chose to report.
    """
    exe = blender_path or BLENDER
    if exe is None:  # pragma: no cover - guarded by requires_blender
        raise RuntimeError("no Blender executable found")
    proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    raw = (proc.stdout or "") + (proc.stderr or "")
    info: dict = {
        "executable": exe,
        "version": None,
        "build_hash": None,
        "build_date": None,
        "raw": raw.strip(),
        "returncode": proc.returncode,
    }
    for line in raw.splitlines():
        stripped = line.strip()
        if info["version"] is None:
            m = re.match(r"^Blender\s+(\S+)", stripped)
            if m:
                info["version"] = m.group(1)
                continue
        low = stripped.lower()
        if low.startswith("build hash:"):
            info["build_hash"] = stripped.split(":", 1)[1].strip()
        elif low.startswith("build date:"):
            info["build_date"] = stripped.split(":", 1)[1].strip()
    return info
