"""Render a blueprint string to a PNG via Factorio-FBSR (game-accurate sprites).

Rendering is **optional** — the POC's correctness comes from the verifier, not the
picture. It shells out to ``scripts/fbsr.sh`` (shipped here), which drives an EXTERNAL
Factorio-FBSR build you provide. Build FBSR from https://github.com/demodude4u/Factorio-FBSR
and point the wrapper at it (it reads ``FBSR_HOME`` / ``FGR_FBSR_HOME``):

    export FBSR_HOME=/path/to/Factorio-FBSR/FactorioBlueprintStringRenderer

Override the wrapper itself with ``FGR_FBSR_SH`` if you have your own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_UPSTREAM = "https://github.com/demodude4u/Factorio-FBSR"
_WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "fbsr.sh"


class RenderError(RuntimeError):
    pass


def fbsr_script() -> Path:
    """The FBSR CLI wrapper (defaults to this repo's ``scripts/fbsr.sh``; override
    with ``FGR_FBSR_SH``)."""
    return Path(os.environ.get("FGR_FBSR_SH", str(_WRAPPER)))


def render_blueprint_string(bp: str, out_png: str | Path, timeout: int = 120) -> Path:
    """Render ``bp`` to ``out_png``. Requires the FBSR service to be running."""
    script = fbsr_script()
    if not script.exists():
        raise RenderError(
            f"FBSR wrapper not found at {script!s}. Rendering is optional; build FBSR "
            f"({_UPSTREAM}) and set FBSR_HOME, or point FGR_FBSR_SH at your own wrapper.")
    # Resolve to an absolute path: FBSR writes relative to *its* own working directory,
    # not ours, so a bare "out.png" would land in the wrong place.
    out = Path(out_png).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["bash", str(script), "bot-render", bp, f"-o={out}", "-full"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not out.exists() or '"success": true' not in proc.stdout:
        raise RenderError(
            f"FBSR render failed (is FBSR built and FBSR_HOME set? see {script.name}).\n"
            f"stdout: {proc.stdout.strip()[-500:]}\nstderr: {proc.stderr.strip()[-500:]}")
    return out
