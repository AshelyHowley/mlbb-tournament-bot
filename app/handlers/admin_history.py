"""Tournament archive: strictly read-only, for MAIN ADMIN and ACTIVE DEPUTY. Nothing here can write -
there is no edit/delete callback for a completed tournament's data anywhere in this router."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.context import Services
from app.handlers.common import IsStaff, show
from app.keyboards import admin as akb
from app.services.errors import ServiceError
from app.utils.text import esc, fmt_dt, parse_int, person

router = Router(name="admin_history")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())


@router.callback_query(F.data.regexp(r"^hist:\d{1,6}$"))
async def history_list(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    total, page, last, rows = await svc.tournaments.history_page(int(cb.data.split(":")[1]))
    text = "📚 <b>История турниров</b>\n\nВсего: <b>{}</b>".format(total) + ("" if total else "\n\nПока нет завершённых турниров.")
    await show(cb, text, akb.history_list(rows, page, last))


@router.callback_query(F.data.regexp(r"^hist:c:\d{1,18}$"))
async def history_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    try:
        row, stats = await svc.tournaments.detail(tid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    first, second, third = None, None, None
    if row["first_place_team_id"]:
        first = (await svc.tournaments.team_card(row["first_place_team_id"]))[0]["name"]
    if row["second_place_team_id"]:
        second = (await svc.tournaments.team_card(row["second_place_team_id"]))[0]["name"]
    if row["third_place_team_id"]:
        third = (await svc.tournaments.team_card(row["third_place_team_id"]))[0]["name"]
    status_line = "🏁 завершён штатно" if row["status"] == "completed" else "⚠️ завершён досрочно"
    lines = [
        "🏆 <b>ИСТОРИЯ ТУРНИРА</b>", "",
        f"📅 Начало: {fmt_dt(row['started_at'], svc.cfg.timezone)}",
        f"🏁 Завершение: {fmt_dt(row['completed_at'], svc.cfg.timezone)} ({status_line})", "",
        f"👑 1 место: {esc(first) if first else 'не определено'}",
        f"🥈 2 место: {esc(second) if second else 'не определено'}",
        f"🥉 3 место: {esc(third) if third else 'не определено'}", "",
        f"👥 Команд: {stats['teams']}", f"👤 Соло-участников: {stats['solo']}",
        f"🎮 Серий сыграно: {stats['series']}", f"🎯 Каток сыграно: {stats['games']}",
    ]
    await show(cb, "\n".join(lines), akb.history_card(tid))


@router.callback_query(F.data.regexp(r"^hist:t:\d{1,18}$"))
async def teams(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    rows = await svc.tournaments.teams(tid)
    text = f"👥 <b>КОМАНДЫ — {len(rows)}</b>" if rows else "👥 <b>Команды</b>\n\nПусто."
    await show(cb, text, akb.history_teams_list(rows, tid))


def _placement_line(team_id: int, tour) -> str:
    if team_id == tour["first_place_team_id"]:
        return "👑 Итог: 1 место / Чемпион"
    if team_id == tour["second_place_team_id"]:
        return "🥈 Итог: 2 место"
    if team_id == tour["third_place_team_id"]:
        return "🥉 Итог: 3 место"
    return ""


@router.callback_query(F.data.regexp(r"^hist:tc:\d{1,18}:\d{1,18}$"))
async def team_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    _, _, team_id, tid = cb.data.split(":")
    team_id, tid = parse_int(team_id), parse_int(tid)
    try:
        team, members, played = await svc.tournaments.team_card(team_id)
    except ServiceError as e:
        await show(cb, e.user_message, akb.history_back(tid))
        return
    tour = await svc.tournaments.get(tid)
    wins = sum(1 for m in played if m["status"] == "finished" and m["winner_id"] == team_id)
    losses = sum(1 for m in played if m["status"] == "finished" and m["winner_id"] and m["winner_id"] != team_id)
    lines = [f"👥 <b>{esc(team['name'])}</b>", ""]
    placement = _placement_line(team_id, tour)
    if placement:
        lines += [placement, ""]
    lines.append(f"👥 Состав: {len(members)} игрок(ов)")
    lines.append("👑 Капитан: " + (next((person(m['full_name'], m['username'], m['user_id']) for m in members if m['is_captain']), "не назначен")))
    lines.append("")
    lines.append("🎮 Игроки:")
    lines += [f"{i}. {person(m['full_name'], m['username'], m['user_id'])}" for i, m in enumerate(members, 1)]
    lines += ["", f"🎮 Сыграно серий: {len(played)}", f"🏆 Побед: {wins}", f"❌ Поражений: {losses}"]
    await show(cb, "\n".join(lines), akb.history_back(tid))


@router.callback_query(F.data.regexp(r"^hist:s:\d{1,18}$"))
async def solo(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    rows = await svc.tournaments.solo(tid)
    if not rows:
        await show(cb, "👤 <b>Соло-участники</b>\n\nПусто.", akb.history_back(tid))
        return
    lines = [f"👤 <b>СОЛО-УЧАСТНИКИ — {len(rows)}</b>", ""]
    lines += [f"{i}. {person(r['full_name'], r['username'], r['user_id'])} — {esc(r['role'])}" for i, r in enumerate(rows, 1)]
    await show(cb, "\n".join(lines), akb.history_back(tid))


@router.callback_query(F.data.regexp(r"^hist:b:\d{1,18}$"))
async def bracket(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    from app.services.bracket import render_bracket

    tid = parse_int(cb.data.split(":")[2])
    rows = await svc.tournaments.bracket(tid)
    pages = render_bracket(rows)
    await show(cb, pages[0], akb.history_back(tid))
    for extra in pages[1:]:
        await cb.bot.send_message(cb.from_user.id, extra)


@router.callback_query(F.data.regexp(r"^hist:m:\d{1,18}$"))
async def matches(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    rows = await svc.tournaments.bracket(tid)
    text = "🎮 <b>МАТЧИ И РЕЗУЛЬТАТЫ</b>" if rows else "🎮 <b>Матчи и результаты</b>\n\nПусто."
    await show(cb, text, akb.history_matches_list(rows, tid))


@router.callback_query(F.data.regexp(r"^hist:mc:\d{1,18}:\d{1,18}$"))
async def match_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    _, _, mid, tid = cb.data.split(":")
    mid, tid = parse_int(mid), parse_int(tid)
    rows = await svc.tournaments.bracket(tid)
    m = next((r for r in rows if r["id"] == mid), None)
    if not m:
        await show(cb, "Матч не найден.", akb.history_back(tid))
        return
    gs = await svc.tournaments.series_games(mid)
    wa = sum(1 for g in gs if g["winner_team_id"] == m["team_a_id"])
    wb = sum(1 for g in gs if g["winner_team_id"] == m["team_b_id"])
    lines = [f"{esc(m['stage_title'])}", "", f"{esc(m['team_a'])}\n{wa} : {wb}\n{esc(m['team_b'])}", "", "🎯 Катки:"]
    lines += [f"{g['game_number']}. 🏆 {esc(g['winner_name'])}" for g in gs]
    if m["status"] == "finished":
        lines += ["", f"🏆 Победитель серии: {esc(m['winner'])}"]
        tour = await svc.tournaments.get(tid)
        placement = _placement_line(m["winner_id"], tour)
        if placement:
            lines.append(placement)
        lines.append(f"📅 Завершён: {fmt_dt(m['finished_at'], svc.cfg.timezone)}")
    await show(cb, "\n".join(lines), akb.history_back(tid))


@router.callback_query(F.data.regexp(r"^hist:stat:\d{1,18}$"))
async def stats(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    row, s = await svc.tournaments.detail(tid)
    breakdown = await svc.tournaments.bo5_breakdown(tid)
    first = second = third = None
    if row["first_place_team_id"]:
        first = (await svc.tournaments.team_card(row["first_place_team_id"]))[0]["name"]
    if row["second_place_team_id"]:
        second = (await svc.tournaments.team_card(row["second_place_team_id"]))[0]["name"]
    if row["third_place_team_id"]:
        third = (await svc.tournaments.team_card(row["third_place_team_id"]))[0]["name"]
    lines = [
        "📊 <b>СТАТИСТИКА ТУРНИРА</b>", "",
        f"👥 Команд: {s['teams']}", f"👤 Соло-участников: {s['solo']}", "",
        f"🎮 Сыграно серий: {s['series']}", f"🎯 Сыграно каток: {s['games']}", "",
        "📊 BO5:",
    ]
    lines += [f"{b['win']}:{b['lose']} — {b['n']}" for b in breakdown] or ["  нет завершённых серий"]
    lines += [
        "",
        f"👑 1 место: {esc(first) if first else 'не определено'}",
        f"🥈 2 место: {esc(second) if second else 'не определено'}",
        f"🥉 3 место: {esc(third) if third else 'не определено'}",
        "",
        f"📅 Начало: {fmt_dt(row['started_at'], svc.cfg.timezone)}",
        f"🏁 Завершение: {fmt_dt(row['completed_at'], svc.cfg.timezone)}",
    ]
    await show(cb, "\n".join(lines), akb.history_back(tid))
