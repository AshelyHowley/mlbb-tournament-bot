"""Bracket image - Sun Straik style."""
from io import BytesIO
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

def _f(sz):
    for name in ["segoeui.ttf", "arial.ttf", "calibri.ttf"]:
        try: return ImageFont.truetype(name, sz)
        except: pass
    return ImageFont.load_default()

def render_bracket_image(matches: list[dict], title: str = "Турнир", subtitle: str = "Single Elimination") -> bytes:
    # Layout
    ML, MR, MT, MB = 60, 60, 90, 60
    COL = 240
    SW, SH = 170, 22
    PH, GAP = 48, 12
    # Colors
    BG = (26, 28, 34)
    FILL = (38, 100, 120)
    FILL_U = (50, 55, 65)
    LN = (90, 95, 105)
    TXT = (230, 235, 240)
    TXT_DIM = (150, 155, 165)
    ICON = (70, 150, 170)
    STAGE = {"playin": "ОТБОР", "r16": "1/16", "r8": "1/8", "r4": "1/4", "semi": "1/2", "final": "ФИНАЛ", "finalist": "ФИНАЛИСТ"}
    if not matches: matches = []
    all_stages = ["playin", "r16", "r8", "r4", "semi", "final", "finalist"]
    by = {}
    for m in matches:
        st = m.get("stage_code") or m.get("stage", "r8")
        if st not in by: by[st] = []
        by[st].append(m)
    stages = [s for s in all_stages if s in by]
    if not stages:
        stages = ["r8"]
        by["r8"] = []
    n_col = len(stages)
    max_n = max((len(by.get(s, [])) for s in stages), default=1)
    W = ML + n_col * COL + MR
    H = MT + max_n * (PH + GAP) + MB
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    fnt = _f(11)
    fnt_t = _f(16)
    fnt_s = _f(10)
    # Title
    d.text((W//2, 18), title, fill=TXT, font=fnt_t, anchor="mt")
    d.text((W//2, 38), subtitle, fill=TXT_DIM, font=fnt_s, anchor="mt")
    positions = {}
    # Draw slots
    for si, st in enumerate(stages):
        matches_st = by[st]
        n = len(matches_st)
        total_h = n * (PH + GAP) - GAP
        start_y = MT + (max_n * (PH + GAP) - total_h) // 2
        x = ML + si * COL
        positions[st] = []
        # Stage label
        d.text((x + SW//2, MT - 18), STAGE.get(st, st.upper()) + " >", fill=TXT_DIM, font=fnt, anchor="mt")
        d.line([(x, MT - 5), (x + SW, MT - 5)], fill=(60, 65, 75), width=1)
        for i, m in enumerate(matches_st):
            y0 = start_y + i * (PH + GAP)
            cy = y0 + PH // 2
            positions[st].append((x, cy, y0))
            a = (m.get("team_a") or "?")[:16]
            b = (m.get("team_b") or "?")[:16]
            if st == "finalist":
                winner = m.get("winner") or (a if a != "?" else "?")
                d.rounded_rectangle([x, y0, x+SW, y0+PH-4], 4, fill=FILL if winner != "?" else FILL_U)
                if winner != "?":
                    d.ellipse([x+4, y0+PH//2-8, x+20, y0+PH//2+8], fill=ICON)
                d.text((x+24, y0+PH//2-6), winner, fill=TXT, font=fnt)
            else:
                # Team A
                d.rounded_rectangle([x, y0, x+SW, y0+SH], 4, fill=FILL if a != "?" else FILL_U)
                if a != "?":
                    d.ellipse([x+4, y0+3, x+18, y0+17], fill=ICON)
                d.text((x+22, y0+3), a, fill=TXT, font=fnt)
                # Team B
                d.rounded_rectangle([x, y0+SH+2, x+SW, y0+PH-2], 4, fill=FILL if b != "?" else FILL_U)
                if b != "?":
                    d.ellipse([x+4, y0+SH+5, x+18, y0+SH+19], fill=ICON)
                d.text((x+22, y0+SH+5), b, fill=TXT, font=fnt)
    # Draw lines
    for si, st in enumerate(stages[:-1]):
        next_st = stages[si + 1]
        if st not in positions or next_st not in positions: continue
        pos = positions[st]
        next_pos = positions[next_st]
        n = len(pos)
        next_n = len(next_pos)
        if n == 0 or next_n == 0: continue
        mid_x = pos[0][0] + SW + 25
        if st == "playin":
            next_matches = by.get(next_st, [])
            empty_idx = [i for i, m in enumerate(next_matches) if (m.get("team_a") or "?") == "?" or (m.get("team_b") or "?") == "?"]
            if not empty_idx: empty_idx = list(range(next_n - min(n, next_n), next_n))
            for i, (x, cy, _) in enumerate(pos):
                if i < len(empty_idx) and empty_idx[i] < next_n:
                    nx, ny, _ = next_pos[empty_idx[i]]
                    d.line([(x + SW, cy), (mid_x, cy)], fill=LN, width=1)
                    d.line([(mid_x, cy), (mid_x, ny)], fill=LN, width=1)
                    d.line([(mid_x, ny), (nx - 2, ny)], fill=LN, width=1)
        else:
            for p_idx in range(min(n // 2, next_n)):
                i = p_idx * 2
                x1, cy1, _ = pos[i]
                cy2 = pos[i+1][1] if i+1 < n else cy1
                nx, ny, _ = next_pos[p_idx]
                mid_y = (cy1 + cy2) // 2
                d.line([(x1 + SW, cy1), (mid_x, cy1)], fill=LN, width=1)
                if i+1 < n:
                    d.line([(pos[i+1][0] + SW, cy2), (mid_x, cy2)], fill=LN, width=1)
                    d.line([(mid_x, cy1), (mid_x, cy2)], fill=LN, width=1)
                d.line([(mid_x, mid_y), (mid_x + 12, mid_y)], fill=LN, width=1)
                d.line([(mid_x + 12, mid_y), (mid_x + 12, ny)], fill=LN, width=1)
                d.line([(mid_x + 12, ny), (nx - 2, ny)], fill=LN, width=1)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
