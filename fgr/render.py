"""Render a blueprint string to a PNG via Factorio-FBSR (game-accurate sprites).

Rendering is **optional** — the POC's correctness comes from the verifier, not the
picture. It shells out to an FBSR CLI wrapper that supports
``<wrapper> bot-render <blueprint-string> -o=<png> -full`` and is backed by a warm
render service. Build FBSR from https://github.com/demodude4u/Factorio-FBSR, then
point this module at your wrapper script:

    export FGR_FBSR_SH=/path/to/your/fbsr.sh
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_UPSTREAM = "https://github.com/demodude4u/Factorio-FBSR"


class RenderError(RuntimeError):
    pass


def fbsr_script() -> Path:
    """The FBSR CLI wrapper. Set ``FGR_FBSR_SH`` to its path."""
    return Path(os.environ.get("FGR_FBSR_SH", "fbsr.sh"))


def render_blueprint_string(bp: str, out_png: str | Path, timeout: int = 120) -> Path:
    """Render ``bp`` to ``out_png``. Requires the FBSR service to be running."""
    script = fbsr_script()
    if not script.exists():
        raise RenderError(
            f"FBSR wrapper not found at {script!s}. Rendering is optional; to enable it, "
            f"build FBSR ({_UPSTREAM}) and set FGR_FBSR_SH to your render wrapper script.")
    # Resolve to an absolute path: the FBSR service writes relative to *its* own
    # working directory, not ours, so a bare "out.png" would land in the wrong place.
    out = Path(out_png).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["bash", str(script), "bot-render", bp, f"-o={out}", "-full"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not out.exists() or '"success": true' not in proc.stdout:
        raise RenderError(
            "FBSR render failed — is the render service running?\n"
            f"stdout: {proc.stdout.strip()[-500:]}\nstderr: {proc.stderr.strip()[-500:]}")
    return out
