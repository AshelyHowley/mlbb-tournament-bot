"""Картинка турнирной сетки (Pillow).

Раскладка строится по реальным связям next_match_id, а не по порядку в списке.
Строки должны содержать: id, stage, team_a, team_b, winner, next_match_id.
Функция синхронная: из async-кода вызывайте через asyncio.to_thread(...).
"""
from __future__ import annotations

import os
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

S = 2                                   # суперсэмплинг: рисуем в 2x, картинка получается чёткой
ML, MR, MT, MB = 50, 50, 80, 50
COL, SW, SH, PH, GAP = 240, 176, 22, 52, 18

BG, FILL, FILL_WIN, FILL_LOSE, FILL_EMPTY = (22, 24, 30), (36, 98, 118), (46, 130, 98), (40, 56, 64), (48, 52, 62)
LN, TXT, TXT_DIM, ICON, GOLD = (90, 100, 110), (230, 235, 240), (140, 150, 160), (60, 140, 160), (222, 184, 70)

STAGE_ORDER = ["playin", "r32", "r16", "r8", "r4", "semi", "final", "finalist"]
STAGE_LABEL = {"playin": "ОТБОР", "r32": "1/32", "r16": "1/16", "r8": "1/8", "r4": "1/4",
               "semi": "1/2", "final": "ФИНАЛ", "finalist": "ЧЕМПИОН"}
THIRD_LABEL = "МАТЧ ЗА 3 МЕСТО"
THIRD_GAP = 34                          # место под подпись над матчем за 3-е место

_FONT_CANDIDATES = [
    os.environ.get("BRACKET_FONT", ""),
    os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "NotoSans-Regular.ttf"),
    "segoeui.ttf", "arial.ttf",                                            # Windows
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",                     # Linux
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",                        # macOS
]


@lru_cache(maxsize=16)
def _font(size: int):
    for path in _FONT_CANDIDATES:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)          # Pillow >= 10.1; кириллицу может не знать


def _fit(text: str, font, max_w: float) -> str:
    """Обрезает по реальной ширине в пикселях и добавляет «…»."""
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text + "…") > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _layout(rows: list[dict]):
    nodes = {r["id"]: dict(r, children=[]) for r in rows if r.get("stage") not in ("finalist", "third")}
    for n in nodes.values():
        parent = nodes.get(n.get("next_match_id"))
        if parent is not None:
            parent["children"].append(n)
    for n in nodes.values():
        n["children"].sort(key=lambda c: c["id"])
    roots = sorted((n for n in nodes.values() if n.get("next_match_id") not in nodes), key=lambda n: n["id"])

    counter = [0]

    def place(n):
        if not n["children"]:
            n["y"] = MT + counter[0] * (PH + GAP)
            counter[0] += 1
        else:
            for c in n["children"]:
                place(c)
            n["y"] = sum(c["y"] for c in n["children"]) / len(n["children"])

    for r in roots:
        place(r)
    return nodes, roots, max(counter[0], 1)


def render_bracket_image(rows: list[dict], title: str = "Турнир", subtitle: str = "Single Elimination") -> bytes:
    rows = list(rows or [])
    champion_row = next((r for r in rows if r.get("stage") == "finalist"), None)
    third_row = next((r for r in rows if r.get("stage") == "third"), None)
    nodes, roots, leaves = _layout(rows)

    stages = [s for s in STAGE_ORDER if s != "finalist" and any(n["stage"] == s for n in nodes.values())]
    if champion_row is not None:
        stages.append("finalist")
    n_col = max(len(stages), 1)

    W = ML + n_col * COL + MR
    H = MT + leaves * (PH + GAP) + MB
    third_y = MT + leaves * (PH + GAP) + THIRD_GAP - GAP
    if third_row is not None:
        H = third_y + PH + MB
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    f, f_t, f_s = _font(11 * S), _font(16 * S), _font(10 * S)
    sc = lambda v: int(round(v * S))

    d.text((sc(W // 2), sc(14)), title, fill=TXT, font=f_t, anchor="mt")
    d.text((sc(W // 2), sc(36)), subtitle, fill=TXT_DIM, font=f_s, anchor="mt")

    col_of = {s: i for i, s in enumerate(stages)}
    for s, i in col_of.items():
        x = ML + i * COL
        d.text((sc(x + SW // 2), sc(MT - 18)), STAGE_LABEL.get(s, s.upper()), fill=TXT_DIM, font=f_s, anchor="mt")
        d.line([(sc(x), sc(MT - 6)), (sc(x + SW), sc(MT - 6))], fill=(55, 60, 70), width=S)

    # --- линии (под коробками) ---
    for n in nodes.values():
        for c in n["children"]:
            cx = ML + col_of[c["stage"]] * COL + SW
            px = ML + col_of[n["stage"]] * COL
            cy, py = c["y"] + PH / 2, n["y"] + PH / 2
            mx = px - 20
            d.line([(sc(cx), sc(cy)), (sc(mx), sc(cy)), (sc(mx), sc(py)), (sc(px), sc(py))], fill=LN, width=S)

    # --- матчи ---
    def slot(x, y, name, state):
        fill = {"win": FILL_WIN, "lose": FILL_LOSE, "empty": FILL_EMPTY}.get(state, FILL)
        d.rounded_rectangle([sc(x), sc(y), sc(x + SW), sc(y + SH)], sc(4), fill=fill)
        label = name or "—"
        if name:
            d.ellipse([sc(x + 5), sc(y + 5), sc(x + 17), sc(y + SH - 5)], fill=GOLD if state == "win" else ICON)
        d.text((sc(x + 23), sc(y + SH / 2)), _fit(label, f, sc(SW - 28)),
               fill=TXT if name and state != "lose" else TXT_DIM, font=f, anchor="lm")

    for n in nodes.values():
        x = ML + col_of[n["stage"]] * COL
        y = n["y"]
        a, b, w = n.get("team_a"), n.get("team_b"), n.get("winner")
        for k, name in enumerate((a, b)):
            state = "empty" if not name else ("win" if w and name == w else "lose" if w else "normal")
            slot(x, y + k * (SH + 2) + (0 if k == 0 else 0), name, state)

    if third_row is not None:
        col = col_of.get("final", max(n_col - 1 - (champion_row is not None), 0))
        x = ML + col * COL
        d.text((sc(x + SW // 2), sc(third_y - 14)), THIRD_LABEL, fill=TXT_DIM, font=f_s, anchor="mt")
        a, b, w = third_row.get("team_a"), third_row.get("team_b"), third_row.get("winner")
        for k, name in enumerate((a, b)):
            state = "empty" if not name else ("win" if w and name == w else "lose" if w else "normal")
            slot(x, third_y + k * (SH + 2), name, state)

    final = next((n for n in roots if n["stage"] == "final"), roots[0] if roots else None)
    if champion_row is not None and final is not None and "final" in col_of:
        x = ML + col_of["finalist"] * COL
        y = final["y"]
        name = champion_row.get("winner") or champion_row.get("team_a") or final.get("winner")
        d.rounded_rectangle([sc(x), sc(y), sc(x + SW), sc(y + PH - 4)], sc(6), fill=FILL_WIN if name else FILL_EMPTY,
                            outline=GOLD if name else None, width=S)
        d.text((sc(x + SW / 2), sc(y + (PH - 4) / 2)), _fit(name or "—", _font(13 * S), sc(SW - 16)),
               fill=TXT, font=_font(13 * S), anchor="mm")
        d.line([(sc(ML + col_of["final"] * COL + SW), sc(y + PH / 2)), (sc(x), sc(y + PH / 2))], fill=LN, width=S)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
