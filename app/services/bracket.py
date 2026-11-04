from __future__ import annotations

from typing import Sequence

from app.utils.text import esc

_LIMIT = 3500


def render_bracket(rows: Sequence) -> list[str]:
    """HTML text pages (each < Telegram's 4096 limit). Rows must be ordered by stage sort_order."""
    if not rows:
        return ["🏆 <b>Турнирная сетка</b>\n\nМатчи пока не созданы."]
    blocks: list[str] = []
    current_stage = None
    for m in rows:
        head = ""
        if m["stage_code"] != current_stage:
            current_stage = m["stage_code"]
            head = f"\n━━━ <b>{esc(m['stage_title'])}</b> ━━━\n"
        if m["status"] == "finished":
            status = f"✅ Победитель: <b>{esc(m['winner'])}</b>"
        else:
            status = "⏳ Ожидается"
        blocks.append(f"{head}#{m['id']} {esc(m['team_a'])} ⚔️ {esc(m['team_b'])}\n     {status}\n")
    pages: list[str] = []
    buf = "🏆 <b>Турнирная сетка</b>\n"
    for b in blocks:
        if len(buf) + len(b) > _LIMIT:
            pages.append(buf)
            buf = ""
        buf += b
    pages.append(buf)
    return pages
