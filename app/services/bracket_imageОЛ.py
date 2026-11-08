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
    
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
