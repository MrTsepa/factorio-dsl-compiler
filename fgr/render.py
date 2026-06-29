"""Render a blueprint string to a PNG via the sibling Factorio-FBSR engine.

FBSR (github.com/demodude4u/Factorio-FBSR) is the same renderer FactorioBin / the
Reddit BlueprintBot use, so we get game-accurate sprites. It renders over an RPC
service that must be warm:

    cd ../factorio-patch-prediction && scripts/fbsr_service.sh &   # leave running

This module just shells out to that repo's ``scripts/fbsr.sh bot-render`` wrapper.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Default to the sibling repo's wrapper; override with FGR_FBSR_SH.
_DEFAULT_FBSR = (Path(__file__).resolve().parents[2]
                 / "factorio-patch-prediction" / "scripts" / "fbsr.sh")


class RenderError(RuntimeError):
    pass


def fbsr_script() -> Path:
    return Path(os.environ.get("FGR_FBSR_SH", str(_DEFAULT_FBSR)))


def render_blueprint_string(bp: str, out_png: str | Path, timeout: int = 120) -> Path:
    """Render ``bp`` to ``out_png``. Requires the FBSR service to be running."""
    script = fbsr_script()
    if not script.exists():
        raise RenderError(
            f"FBSR wrapper not found at {script}. Set FGR_FBSR_SH to the path of "
            "factorio-patch-prediction/scripts/fbsr.sh.")
    # Resolve to an absolute path: the FBSR service writes relative to *its* own
    # working directory, not ours, so a bare "out.png" would land in the wrong place.
    out = Path(out_png).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["bash", str(script), "bot-render", bp, f"-o={out}", "-full"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not out.exists() or '"success": true' not in proc.stdout:
        raise RenderError(
            "FBSR render failed. Is the service running "
            "(factorio-patch-prediction/scripts/fbsr_service.sh)?\n"
            f"stdout: {proc.stdout.strip()[-500:]}\nstderr: {proc.stderr.strip()[-500:]}")
    return out
