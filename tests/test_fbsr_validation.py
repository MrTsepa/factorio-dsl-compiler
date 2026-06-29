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
