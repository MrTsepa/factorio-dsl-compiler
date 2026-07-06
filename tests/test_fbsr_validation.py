"""Validate the verifier's geometric model against Factorio's real data via FBSR.

This needs FBSR's prototype dumps (or the FBSR CLI to generate them), so it
auto-skips where that toolchain isn't present — keeping the rest of the suite
hermetic. See `fgr/fbsr_validation.py`.
"""

import pytest

from fgr.fbsr_validation import FbsrUnavailable, validate


def test_verifier_model_matches_factorio_data():
    try:
        checks = validate()
    except FbsrUnavailable as exc:
        pytest.skip(f"FBSR data unavailable: {exc}")
    bad = [c for c in checks if not c.ok]
    assert not bad, "\n".join(f"{c.name}: {c.detail}" for c in bad)


def test_curated_examples_are_game_accurate():
    """GUARDRAIL: every curated spec (examples/basic + examples/complex) must use real
    recipes fed their real ingredients on the right channels -- these are the cases we
    present as playable. Synthetic-recipe stress lives in examples/stress and
    corner_cases (exempt). Skips when FBSR game data is unavailable."""
    import glob
    import os

    from fgr import fbsr_validation as fv
    from fgr.dsl import parse as _parse

    try:
        dumper = fv._fbsr_dumper()
        if dumper is None:
            pytest.skip("FBSR unavailable")
    except Exception:
        pytest.skip("FBSR unavailable")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    for pat in ("examples/basic/*.fgr", "examples/complex/*.fgr"):
        for f in sorted(glob.glob(os.path.join(root, pat))):
            g = _parse(open(f).read())
            try:
                audit = (fv.check_recipes(g, dumper=dumper)
                         + fv.check_ingredients(g, dumper=dumper))
            except fv.FbsrUnavailable:
                pytest.skip("FBSR data unavailable")
            bad += [f"{os.path.basename(f)}: {c.detail}" for c in audit if not c.ok]
    assert not bad, "\n".join(bad)
