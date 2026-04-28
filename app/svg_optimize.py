"""vpype subprocess wrapper for SVG optimization.

The plot worker invokes ``optimize_svg`` before planning when a job has
``optimize=True``. The pipeline composes a vpype command line from the four
toggles (linemerge / linesimplify / linesort / reloop); tolerance feeds the
two commands that accept it.

vpype's default ``read``/``write`` round-trip preserves Inkscape layer
structure (``inkscape:groupmode="layer"`` and ``inkscape:label``), so
downstream ``svg_utils.filter_to_layers`` keeps working on the optimized file.
"""
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class OptimizeError(RuntimeError):
    """Raised when vpype exits non-zero."""


_current_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()


def _vpype_cmd() -> list[str]:
    """Locate the vpype CLI. Prefer the venv binary; fall back to module form."""
    venv_bin = Path(sys.executable).parent / "vpype"
    if venv_bin.is_file():
        return [str(venv_bin)]
    return [sys.executable, "-m", "vpype_cli"]


def build_pipeline(
    src: Path,
    dst: Path,
    tolerance_mm: float,
    linemerge: bool,
    linesimplify: bool,
    linesort: bool,
    reloop: bool,
) -> list[str]:
    cmd = _vpype_cmd()
    cmd += ["read", str(src)]
    if linemerge:
        cmd += ["linemerge", "--tolerance", f"{tolerance_mm}mm"]
    if linesimplify:
        cmd += ["linesimplify", "--tolerance", f"{tolerance_mm}mm"]
    if linesort:
        cmd += ["linesort"]
    if reloop:
        cmd += ["reloop"]
    cmd += ["write", str(dst)]
    return cmd


def optimize_svg(
    src: Path,
    dst: Path,
    tolerance_mm: float,
    linemerge: bool,
    linesimplify: bool,
    linesort: bool,
    reloop: bool,
) -> None:
    """Produce ``dst`` from ``src``. Raises ``OptimizeError`` on vpype failure."""
    if not any([linemerge, linesimplify, linesort, reloop]):
        # No-op pipeline: copy source to keep downstream code uniform.
        shutil.copyfile(src, dst)
        return
    cmd = build_pipeline(src, dst, tolerance_mm, linemerge, linesimplify, linesort, reloop)
    log.info("optimize: %s", " ".join(cmd))
    env = {**os.environ, "VPYPE_NO_COLOR": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    with _proc_lock:
        global _current_proc
        _current_proc = proc
    try:
        stdout, stderr = proc.communicate()
    finally:
        with _proc_lock:
            _current_proc = None
    if proc.returncode != 0:
        msg = (stderr.strip() or stdout.strip() or
               f"vpype exited with code {proc.returncode}")
        # Keep it short — multi-line tracebacks just bloat the UI error pill.
        first_line = msg.splitlines()[-1] if msg else f"rc={proc.returncode}"
        raise OptimizeError(first_line)


def cancel_current() -> None:
    """Kill the in-flight vpype subprocess, if any. Called from the cancel path."""
    with _proc_lock:
        proc = _current_proc
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:
        log.exception("optimize: terminate failed")
