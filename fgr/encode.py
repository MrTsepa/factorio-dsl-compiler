"""Encode/decode Factorio blueprint strings (self-contained, stdlib only).

A blueprint string is ``<version-byte><base64( zlib( utf8-json ) )>`` where the
version byte is ``"0"``. This mirrors the helper in the sibling
``factorio-blueprint-generator`` repo so this POC has no external deps.
"""

from __future__ import annotations

import base64
import json
import zlib


def encode_blueprint_string(obj: dict, version_byte: str = "0") -> str:
    """Serialize a blueprint dict into an importable blueprint string."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return version_byte + base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode_blueprint_string(s: str) -> dict:
    """Decode a Factorio blueprint string back into a dict (handy for tests)."""
    s = s.strip()
    raw = base64.b64decode(s[1:], validate=False)
    return json.loads(zlib.decompress(raw))
