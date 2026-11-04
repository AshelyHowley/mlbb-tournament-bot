"""Large canvas bracket."""
from __future__ import annotations
from io import BytesIO
from typing import Sequence
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

def _f(sz, bold=False):
    try: return ImageFont.truetype("arial.ttf", sz)
    except: return ImageFont.load_default()

def render_bracket_image(matches: Sequence[dict], title: str = "Турнир") -> bytes | None:
    if Image is None or not matches: return None
    cmap = {"r32":"r32","r16":"r16","1/16":"r16","r8":"r8","1/8":"r8","r4":"r4","1/4":"r4","semi":"semi","Полуфинал":"semi","final":"final","Финал":"final","playin":"playin","Отбор":"playin"}
    order = ["playin","r32","r16","r8","r4","semi","final"]
    labs = {"playin":"ОТБОР","r32":"1/32","r16":"1/16","r8":"1/8","r4":"1/4","semi":"1/2","final":"ФИНАЛ"}
    by = {}
    for m in matches:
        c = cmap.get(str(m.get("stage") or ""), "")
        if c: by.setdefault(c, []).append(m)
    stages = [s for s in order if s in by]
    if not stages: return None

    SW, SH = 160, 20
    PH = SH * 2 + 6
    GAP = 14
    COL = SW + 50
    ML, MT = 30, 50
    n0 = len(by[stages[0]])
    H = MT + n0 * (PH + GAP) + 60
    W = ML + len(stages) * COL + 40

    img = Image.new("RGB", (W, H), (18, 20, 26))
    d = ImageDraw.Draw(img)
    fnt = _f(10)
    tw = d.textlength(title, font=_f(14, True))
    d.text(((W - tw) / 2, 8), title, fill=(235, 235, 240), font=_f(14, True))

    pos = {}
    y = MT
    for i in range(n0):
        pos[(stages[0], i)] = y + PH // 2
        y += PH + GAP
    for si in range(1, len(stages)):
        st, prev = stages[si], stages[si - 1]
        for i in range(len(by.get(st, []))):
            c1 = pos.get((prev, i * 2), MT)
            c2 = pos.get((prev, i * 2 + 1), c1)
            pos[(st, i)] = (c1 + c2) // 2

    FILL, FILL_W, FILL_U = (42, 90, 108), (52, 120, 140), (32, 36, 44)
    LN = (55, 62, 75)

    for si, st in enumerate(stages):
        x = ML + si * COL
        d.text((x, MT - 16), f"{labs.get(st, st)} >", fill=(160, 165, 175), font=_f(9))
        for i, m in enumerate(by.get(st, [])):
            cy = pos.get((st, i), MT)
            y0 = cy - PH // 2
            a = str(m.get("team_a") or "?")
            b = str(m.get("team_b") or "?")
            if a in ("None", "TBD"): a = "?"
            if b in ("None", "TBD"): b = "?"
            a, b = a[:16], b[:16]
            win = m.get("winner")
            fa = FILL_W if win and win == m.get("team_a") else (FILL if a != "?" else FILL_U)
            fb = FILL_W if win and win == m.get("team_b") else (FILL if b != "?" else FILL_U)
            d.rounded_rectangle([x, y0, x + SW, y0 + SH], 3, fill=fa)
            d.text((x + 6, y0 + 3), a, fill=(220, 225, 230), font=fnt)
            d.rounded_rectangle([x, y0 + SH + 2, x + SW, y0 + PH - 1], 3, fill=fb)
            d.text((x + 6, y0 + SH + 5), b, fill=(200, 205, 210), font=fnt)
            if si < len(stages) - 1 and (stages[si + 1], i // 2) in pos:
                rx = x + SW
                ny = pos[(stages[si + 1], i // 2)]
                d.line([(rx, cy), (rx + 16, cy)], fill=LN, width=1)
                d.line([(rx + 16, min(cy, ny)), (rx + 16, max(cy, ny))], fill=LN, width=1)
                d.line([(rx + 16, ny), (x + COL - 4, ny)], fill=LN, width=1)

    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
