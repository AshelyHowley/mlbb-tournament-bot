"""Bracket image."""
from io import BytesIO
from typing import Sequence
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

def _f(sz):
    try: return ImageFont.truetype("arial.ttf", sz)
    except: return ImageFont.load_default()

def render_bracket_image(matches: Sequence[dict], title: str = "Турнир") -> bytes | None:
    if Image is None or not matches: return None
    
    order = ["playin", "r16", "r8", "r4", "semi", "final", "finalist"]
    labs = {"playin": "ОТБОР", "r16": "1/16", "r8": "1/8", "r4": "1/4", "semi": "1/2", "final": "ФИНАЛ", "finalist": "ФИНАЛИСТ"}
    cmap = {"Отбор": "playin", "1/16": "r16", "1/8": "r8", "1/4": "r4", "Полуфинал": "semi", "1/2": "semi", "Финал": "final", "Финалист": "finalist"}
    
    by = {}
    for m in matches:
        stage = str(m.get("stage") or "").strip()
        code = cmap.get(stage, stage.lower())
        by.setdefault(code, []).append(m)
    
    stages = [s for s in order if s in by]
    if not stages: return None
    
    SW, SH = 140, 16
    PH = SH * 2 + 4
    GAP = 8
    COL = SW + 36
    ML, MT = 16, 36
    
    # Height = max matches in any stage
    max_n = max(len(by[s]) for s in stages)
    H = MT + max_n * (PH + GAP) + 30
    W = ML + len(stages) * COL + 16
    
    img = Image.new("RGB", (W, H), (18, 20, 26))
    d = ImageDraw.Draw(img)
    fnt = _f(8)
    d.text((W//2 - 20, 6), title, fill=(235, 235, 240), font=_f(11))
    
    FILL, FILL_U = (42, 90, 108), (32, 36, 44)
    LN = (55, 62, 75)
    
    for si, st in enumerate(stages):
        x = ML + si * COL
        d.text((x, MT - 12), labs.get(st, st), fill=(160, 165, 175), font=_f(7))
        matches_st = by[st]
        n = len(matches_st)
        total_h = n * (PH + GAP) - GAP
        start_y = MT + (max_n * (PH + GAP) - total_h) // 2
        
        for i, m in enumerate(matches_st):
            y0 = start_y + i * (PH + GAP)
            
            if st == "finalist":
                d.rounded_rectangle((x, y0, x+SW, y0+SH), 2, fill=FILL_U, outline=LN)
                d.text((x+3, y0+2), m.get("winner") or "?", fill=(200,200,205), font=fnt)
                continue
            
            a = str(m.get("team_a") or "?")[:12]
            b = str(m.get("team_b") or "?")[:12]
            if a in ("None", "TBD"): a = "?"
            if b in ("None", "TBD"): b = "?"
            
            d.rounded_rectangle([x, y0, x+SW, y0+SH], 2, fill=FILL if a != "?" else FILL_U)
            d.text((x+3, y0+1), a, fill=(220,225,230), font=fnt)
            d.rounded_rectangle([x, y0+SH+2, x+SW, y0+PH-2], 2, fill=FILL if b != "?" else FILL_U)
            d.text((x+3, y0+SH+3), b, fill=(200,205,210), font=fnt)
            pass  # lines drawn separately
    
    # Draw connecting lines
    for si, st in enumerate(stages[:-1]):
        next_st = stages[si + 1]
        matches_st = by[st]
        next_matches = by.get(next_st, [])
        n = len(matches_st)
        next_n = len(next_matches)
        if next_n == 0: continue
        x = ML + si * COL
        nx = ML + (si + 1) * COL
        total_h = n * (PH + GAP) - GAP
        next_total_h = next_n * (PH + GAP) - GAP
        start_y = MT + (max_n * (PH + GAP) - total_h) // 2
        next_start_y = MT + (max_n * (PH + GAP) - next_total_h) // 2
        for i in range(0, n, 2):
            next_i = i // 2
            if next_i >= next_n: break
            y0 = start_y + i * (PH + GAP)
            cy1 = y0 + PH // 2
            cy2 = start_y + (i + 1) * (PH + GAP) + PH // 2 if i + 1 < n else cy1
            ny = next_start_y + next_i * (PH + GAP) + PH // 2
            mid_x = x + SW + 16
            d.line([(x + SW, cy1), (mid_x, cy1)], fill=LN)
            if cy2 != cy1:
                d.line([(x + SW, cy2), (mid_x, cy2)], fill=LN)
                d.line([(mid_x, min(cy1, cy2)), (mid_x, max(cy1, cy2))], fill=LN)
            d.line([(mid_x, ny), (nx - 2, ny)], fill=LN)
    
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
