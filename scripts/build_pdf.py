#!/usr/bin/env python
"""Render EVERY example (passing AND failing) full-size via FBSR, stamp each with its
verifier verdict + error context, and assemble one multi-page PDF for review.

    export FBSR_HOME=/path/to/Factorio-FBSR/FactorioBlueprintStringRenderer
    .venv/bin/python scripts/build_pdf.py            # -> out/all_cases.pdf
    .venv/bin/python scripts/build_pdf.py 3          # only first 3 cases (quick test)

One page per case: a header band (name, PASS/FAIL/ERROR, every failing check with detail,
plus the DSL spec) above the full-resolution layout render. A cover page summarises the
pass rate and lists the failing cases so they're easy to find.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont                 # noqa: E402

from fgr.dsl import DslError, parse                         # noqa: E402
from fgr.layout import LayoutError, compile_graph           # noqa: E402
from fgr.verify import verify                               # noqa: E402
from fgr.blueprint import to_blueprint_string               # noqa: E402
from fgr.render import render_blueprint_string              # noqa: E402

OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
TMP = OUT / "_pdf_png"
TMP.mkdir(exist_ok=True)

PAGE_W = 2200            # page width in px; renders are scaled to fit this
MARGIN = 36
GAP = 14
IMG_MAX_H = 2600         # cap a single render's height so giant layouts stay one page-ish

_FONTS = "/System/Library/Fonts/Supplemental/"
F_TITLE = ImageFont.truetype(_FONTS + "Arial Bold.ttf", 46)
F_BADGE = ImageFont.truetype(_FONTS + "Arial Bold.ttf", 34)
F_HEAD = ImageFont.truetype(_FONTS + "Arial Bold.ttf", 30)
F_BODY = ImageFont.truetype(_FONTS + "Arial.ttf", 27)
F_MONO = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 22)

COLOR = {"PASS": (22, 130, 40), "VERIFY-FAIL": (190, 30, 30), "COMPILE-ERROR": (120, 10, 10)}


def _wrap(draw, text, font, maxw):
    lines = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        line = ""
        for word in para.split(" "):
            trial = (line + " " + word).strip()
            if draw.textlength(trial, font=font) <= maxw or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


_M = ImageDraw.Draw(Image.new("RGB", (1, 1)))               # scratch for text measuring


def _text_block(lines, font, maxw):
    """Return (wrapped_lines, height) for a list of raw lines wrapped to maxw."""
    out = []
    for ln in lines:
        out.extend(_wrap(_M, ln, font, maxw))
    lh = font.size + 8
    return out, len(out) * lh, lh


def _evaluate(fp):
    rel = fp.relative_to(ROOT / "examples").as_posix()
    text = fp.read_text()
    rec = {"rel": rel, "dsl": text, "status": "COMPILE-ERROR", "checks": [], "err": None, "img": None}
    try:
        g = parse(text)
        lay = compile_graph(g)
        rep = verify(g, lay)
        rec["status"] = "PASS" if rep.ok else "VERIFY-FAIL"
        rec["checks"] = [(c.name, c.detail) for c in rep.checks if not c.ok]
        bp = to_blueprint_string(lay, rel)
        png = TMP / (rel.replace("/", "_") + ".png")
        try:
            render_blueprint_string(bp, png)
            rec["img"] = png
        except Exception as e:                              # noqa: BLE001
            rec["err"] = f"render failed: {e}"
    except (LayoutError, DslError) as e:
        rec["err"] = f"{type(e).__name__}: {e}"
    except Exception as e:                                  # noqa: BLE001
        rec["err"] = f"{type(e).__name__}: {e}"
    return rec


def _compose(rec):
    """Build one page image: header (name/status/checks/dsl) over the render."""
    inner = PAGE_W - 2 * MARGIN
    status = rec["status"]
    # ---- header text blocks ----
    head_lines = []
    if status != "PASS":
        if rec["checks"]:
            head_lines.append(("HEAD", "Failing checks:"))
            for name, detail in rec["checks"]:
                head_lines.append(("BODY", f"  • {name}: {detail}"))
        if rec["err"]:
            head_lines.append(("HEAD", "Error:"))
            head_lines.append(("BODY", "  " + rec["err"]))
    head_lines.append(("HEAD", "Spec (DSL):"))
    for ln in rec["dsl"].strip().split("\n"):
        head_lines.append(("MONO", "  " + ln))

    # measure
    h = MARGIN + F_TITLE.size + 18
    rendered = []
    for kind, txt in head_lines:
        font = {"HEAD": F_HEAD, "BODY": F_BODY, "MONO": F_MONO}[kind]
        wl, hh, lh = _text_block([txt], font, inner)
        rendered.append((font, wl, lh))
        h += hh + 4
    header_h = h + GAP

    # ---- image ----
    img = None
    if rec["img"] is not None:
        img = Image.open(rec["img"]).convert("RGB")
        scale = min(inner / img.width, IMG_MAX_H / img.height)
        if scale < 1:
            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                             Image.LANCZOS)
        img_h = img.height
    else:
        img_h = 0

    page = Image.new("RGB", (PAGE_W, header_h + img_h + MARGIN), (255, 255, 255))
    d = ImageDraw.Draw(page)
    y = MARGIN
    # title + status badge
    d.text((MARGIN, y), rec["rel"], font=F_TITLE, fill=(20, 20, 20))
    badge = status
    bw = d.textlength(badge, font=F_BADGE)
    d.rectangle([PAGE_W - MARGIN - bw - 24, y, PAGE_W - MARGIN, y + F_BADGE.size + 16],
                fill=COLOR.get(status, (90, 90, 90)))
    d.text((PAGE_W - MARGIN - bw - 12, y + 8), badge, font=F_BADGE, fill=(255, 255, 255))
    y += F_TITLE.size + 18
    for (font, wl, lh) in rendered:
        for line in wl:
            col = (150, 40, 40) if line.strip().startswith("•") else (40, 40, 40)
            d.text((MARGIN, y), line, font=font, fill=col)
            y += lh
        y += 4
    if img is not None:
        page.paste(img, (MARGIN, header_h))
    else:
        d.text((MARGIN, header_h), "(no render — see error above)", font=F_HEAD, fill=(150, 40, 40))
    return page


def _cover(recs):
    inner = PAGE_W - 2 * MARGIN
    by_suite = {}
    for r in recs:
        by_suite.setdefault(r["rel"].split("/")[0], []).append(r)
    npass = sum(r["status"] == "PASS" for r in recs)
    lines = [("TITLE", f"fgr — all {len(recs)} cases   ({npass}/{len(recs)} verify)"),
             ("BODY", time.strftime("generated %Y-%m-%d %H:%M")), ("BODY", "")]
    for suite in ("basic", "complex", "stress"):
        rs = by_suite.get(suite, [])
        if rs:
            p = sum(r["status"] == "PASS" for r in rs)
            lines.append(("HEAD", f"{suite}: {p}/{len(rs)} verify"))
    lines.append(("BODY", ""))
    fails = [r for r in recs if r["status"] != "PASS"]
    lines.append(("HEAD", f"Failing cases ({len(fails)}):"))
    for r in fails:
        first = (r["checks"][0][1] if r["checks"] else r["err"]) or ""
        lines.append(("BODY", f"  • {r['rel']} — {first}"))

    blocks, h = [], MARGIN
    for kind, txt in lines:
        font = {"TITLE": F_TITLE, "HEAD": F_HEAD, "BODY": F_BODY}[kind]
        wl, hh, lh = _text_block([txt], font, inner)
        blocks.append((font, wl, lh, kind))
        h += hh + 6
    page = Image.new("RGB", (PAGE_W, h + MARGIN), (255, 255, 255))
    d = ImageDraw.Draw(page)
    y = MARGIN
    for font, wl, lh, kind in blocks:
        for line in wl:
            col = (150, 40, 40) if line.strip().startswith("•") else (20, 20, 20)
            d.text((MARGIN, y), line, font=font, fill=col)
            y += lh
        y += 6
    return page


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    examples = sorted((ROOT / "examples").glob("*/*.fgr"))
    if limit:
        examples = examples[:limit]
    recs = []
    for i, fp in enumerate(examples, 1):
        rec = _evaluate(fp)
        recs.append(rec)
        print(f"[{i}/{len(examples)}] {rec['rel']:32s} {rec['status']}"
              + ("" if rec["img"] else "  (no image)"))
    pages = [_cover(recs)] + [_compose(r) for r in recs]
    out_pdf = OUT / "all_cases.pdf"
    pages[0].save(out_pdf, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    mb = out_pdf.stat().st_size / 1e6
    print(f"\nwrote {out_pdf} ({mb:.1f} MB, {len(pages)} pages)")


if __name__ == "__main__":
    main()
