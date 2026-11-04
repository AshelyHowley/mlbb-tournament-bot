from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery

from app.bot.context import Services
from app.handlers.common import IsStaff, show, tg_user
from app.keyboards import admin as akb
from app.services.errors import ServiceError
from app.services.series import SeriesView
from app.utils.text import esc, parse_int

router = Router(name="admin_matches")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

MATCHES_TITLE = "⚔️ <b>Матчи</b>"


class MatchCreate(StatesGroup):
    team_a = State()
    team_b = State()
    stage = State()
    confirm = State()


@router.callback_query(F.data == "adm:m")
async def matches_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await show(cb, MATCHES_TITLE, akb.matches_menu())


@router.callback_query(F.data == "mt:cancel")
async def cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    await state.clear()
    await show(cb, MATCHES_TITLE, akb.matches_menu())


# ------------------------------------------------------------------ creation flow
async def _show_teams(cb: CallbackQuery, svc: Services, prefix: str, page: int, exclude: int, title: str) -> None:
    total, page, last, rows = await svc.admin.teams_page(page)
    if total < 2:
        await show(cb, "❌ Для матча нужно минимум 2 зарегистрированные команды.", akb.matches_menu())
        return
    await show(cb, title, akb.pick_team(rows, prefix, page, last, exclude))


@router.callback_query(F.data == "mt:new")
async def new_match(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(MatchCreate.team_a)
    await _show_teams(cb, svc, "mt:a", 0, 0, "⚔️ <b>Новый матч</b>\nВыберите <b>Team A</b>:")


@router.callback_query(MatchCreate.team_a, F.data.regexp(r"^mt:a:pg:\d{1,6}$"))
async def a_page(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    await _show_teams(cb, svc, "mt:a", int(cb.data.split(":")[3]), 0, "⚔️ <b>Новый матч</b>\nВыберите <b>Team A</b>:")


@router.callback_query(MatchCreate.team_a, F.data.regexp(r"^mt:a:pick:\d{1,18}$"))
async def a_pick(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    a = parse_int(cb.data.split(":")[3])
    try:
        await svc.admin.team_card(a)
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        await state.clear()
        return
    await state.update_data(a=a)
    await state.set_state(MatchCreate.team_b)
    await _show_teams(cb, svc, "mt:b", 0, a, "Выберите <b>Team B</b>:")


@router.callback_query(MatchCreate.team_b, F.data.regexp(r"^mt:b:pg:\d{1,6}$"))
async def b_page(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    a = (await state.get_data())["a"]
    await _show_teams(cb, svc, "mt:b", int(cb.data.split(":")[3]), a, "Выберите <b>Team B</b>:")


@router.callback_query(MatchCreate.team_b, F.data.regexp(r"^mt:b:pick:\d{1,18}$"))
async def b_pick(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    b = parse_int(cb.data.split(":")[3])
    a = (await state.get_data())["a"]
    if b == a:
        await cb.answer("Команда не может играть сама с собой", show_alert=True)
        return
    await cb.answer()
    try:
        await svc.admin.team_card(b)
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        await state.clear()
        return
    await state.update_data(b=b)
    await state.set_state(MatchCreate.stage)
    await show(cb, "Выберите <b>этап</b> турнира:", akb.pick_stage(await svc.admin.stages()))


@router.callback_query(MatchCreate.stage, F.data.regexp(r"^mt:stage:[a-z0-9_]{1,20}$"))
async def pick_stage(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    code = cb.data.split(":")[2]
    stages = {s["code"]: s["title"] for s in await svc.admin.stages()}
    if code not in stages:
        return
    data = await state.get_data()
    try:
        ta, _ = await svc.admin.team_card(data["a"])
        tb, _ = await svc.admin.team_card(data["b"])
    except ServiceError as e:
        await state.clear()
        await show(cb, e.user_message, akb.matches_menu())
        return
    await state.update_data(stage=code)
    await state.set_state(MatchCreate.confirm)
    await show(
        cb,
        f"📋 <b>Создать матч?</b>\n\nЭтап: <b>{esc(stages[code])}</b>\nTeam A: <b>{esc(ta['name'])}</b>\nTeam B: <b>{esc(tb['name'])}</b>\n\n"
        "Матч проводится до 3 побед (Best of 5).",
        akb.confirm_match(),
    )


@router.callback_query(MatchCreate.confirm, F.data == "mt:ok")
async def confirm(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    data = await state.get_data()
    await state.clear()  # cleared first: a repeated callback lands on the stale-button fallback
    try:
        mid = await svc.admin.create_match(data["a"], data["b"], data["stage"])
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    await show(cb, f"✅ Матч #{mid} создан.", akb.matches_menu())


# ------------------------------------------------------------------ list / series card
def _series_text(v: SeriesView, tz: str) -> str:
    m = v.match
    lines = [f"🎮 <b>{esc(m['stage_title'])}</b>", "", f"{esc(m['team_a'])}", "🆚", f"{esc(m['team_b'])}", ""]
    lines.append(f"📊 Счёт серии: {v.wins_a} : {v.wins_b}")
    if v.games:
        lines.append("")
        lines.append("🎯 Катки:")
        lines += [f"{g['game_number']}. 🏆 {esc(g['winner_name'])}" for g in v.games]
    if m["status"] == "finished":
        lines += ["", f"🏁 <b>МАТЧ ЗАВЕРШЁН</b>\n\n{esc(m['team_a'])} {v.wins_a} : {v.wins_b} {esc(m['team_b'])}",
                   f"\n🏆 Победитель серии: <b>{esc(m['winner'])}</b>"]
    else:
        lines += ["", f"Следующая:\n🎮 Катка №{v.next_game_number}"]
    return "\n".join(lines)


@router.callback_query(F.data.regexp(r"^mt:l:(scheduled|finished):\d{1,6}$"))
async def match_list(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await state.clear()
    _, _, status, page = cb.data.split(":")
    total, page, last, rows = await svc.admin.matches_page(status, int(page))
    title = "⏳ Ожидающие матчи" if status == "scheduled" else "✅ Завершённые матчи"
    await show(cb, f"<b>{title}</b>\nВсего: <b>{total}</b>", akb.match_list(rows, status, page, last))


@router.callback_query(F.data.regexp(r"^mt:c:\d{1,18}$"))
async def match_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        v = await svc.series.series_view(parse_int(cb.data.split(":")[2]))
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    await show(cb, _series_text(v, svc.cfg.timezone), akb.series_card(v.match))


@router.callback_query(F.data.regexp(r"^mt:d:\d{1,18}$"))
async def delete_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    mid = parse_int(cb.data.split(":")[2])
    await show(
        cb, f"⚠️ Удалить матч #{mid}? Это действие необратимо.", akb.confirm_delete(f"mt:dok:{mid}", f"mt:c:{mid}")
    )


@router.callback_query(F.data.regexp(r"^mt:dok:\d{1,18}$"))
async def delete_ok(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        await svc.admin.delete_match(parse_int(cb.data.split(":")[2]))
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    await show(cb, "✅ Матч удалён.", akb.matches_menu())


# ------------------------------------------------------------------ enter a game result (BO5)
@router.callback_query(F.data.regexp(r"^mt:gnew:\d{1,18}$"))
async def game_new(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        v = await svc.series.series_view(parse_int(cb.data.split(":")[2]))
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    if v.match["status"] == "finished":
        await show(cb, "ℹ️ Серия уже завершена.", akb.series_card(v.match))
        return
    await show(cb, f"🎮 <b>КАТКА №{v.next_game_number}</b>\n\n{esc(v.match['team_a'])}\n🆚\n{esc(v.match['team_b'])}\n\nКто победил?", akb.game_winner_pick(v.match))


@router.callback_query(F.data.regexp(r"^mt:gw:\d{1,18}:\d{1,18}$"))
async def game_pick(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    mid, tid = (parse_int(x) for x in cb.data.split(":")[2:])
    try:
        v = await svc.series.series_view(mid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    if tid not in (v.match["team_a_id"], v.match["team_b_id"]):
        await show(cb, "❌ Некорректный выбор победителя.", akb.series_card(v.match))
        return
    name = v.match["team_a"] if tid == v.match["team_a_id"] else v.match["team_b"]
    await show(
        cb, f"⚠️ <b>ПОДТВЕРДИТЕ РЕЗУЛЬТАТ</b>\n\nКатка №{v.next_game_number}\n\nПобедитель:\n<b>{esc(name)}</b>",
        akb.confirm_game(mid, tid, v.next_game_number),
    )


@router.callback_query(F.data.regexp(r"^mt:gok:\d{1,18}:\d{1,18}:\d{1,2}$"))
async def game_confirm(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    mid, tid, game_no = cb.data.split(":")[2:]
    mid, tid, game_no = parse_int(mid), parse_int(tid), parse_int(game_no)
    try:
        v = await svc.series.add_game(tg_user(cb), mid, tid, expected_game_number=game_no)
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    prefix = "✅ Результат сохранён.\n\n" if v.match["status"] != "finished" else ""
    await show(cb, prefix + _series_text(v, svc.cfg.timezone), akb.series_card(v.match))


# ------------------------------------------------------------------ correct a past game result
@router.callback_query(F.data.regexp(r"^mt:gedit:\d{1,18}$"))
async def game_edit_list(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        v = await svc.series.series_view(parse_int(cb.data.split(":")[2]))
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    if not v.games:
        await show(cb, "ℹ️ В этой серии пока нет каток.", akb.series_card(v.match))
        return
    await show(cb, "✏️ <b>Исправить результат</b>\n\nВыберите катку:", akb.games_edit_list(v.match["id"], v.games))


@router.callback_query(F.data.regexp(r"^mt:gec:\d{1,18}$"))
async def game_edit_pick(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    game_id = parse_int(cb.data.split(":")[2])
    g, v = await svc.series.game_and_series(game_id)
    if not g:
        await show(cb, "Катка не найдена.", akb.matches_menu())
        return
    game = next((x for x in v.games if x["id"] == game_id), None)
    await show(
        cb, f"✏️ <b>Катка №{game['game_number']}</b>\n\nТекущий победитель: {esc(game['winner_name'])}\n\nВыберите нового победителя:",
        akb.game_edit_pick(game, v.match),
    )


@router.callback_query(F.data.regexp(r"^mt:gew:\d{1,18}:\d{1,18}$"))
async def game_edit_confirm_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    game_id, tid = (parse_int(x) for x in cb.data.split(":")[2:])
    g, v = await svc.series.game_and_series(game_id)
    if not g:
        await show(cb, "Катка не найдена.", akb.matches_menu())
        return
    if tid not in (v.match["team_a_id"], v.match["team_b_id"]):
        await show(cb, "❌ Некорректный выбор победителя.", akb.series_card(v.match))
        return
    name = v.match["team_a"] if tid == v.match["team_a_id"] else v.match["team_b"]
    await show(
        cb, f"⚠️ Изменить победителя катки №{g['game_number']} на <b>{esc(name)}</b>?",
        akb.confirm_game_edit(game_id, tid, v.match["id"]),
    )


@router.callback_query(F.data.regexp(r"^mt:gewok:\d{1,18}:\d{1,18}$"))
async def game_edit_ok(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    game_id, tid = (parse_int(x) for x in cb.data.split(":")[2:])
    g, _v = await svc.series.game_and_series(game_id)
    if not g:
        await show(cb, "Катка не найдена.", akb.matches_menu())
        return
    try:
        v = await svc.series.edit_game(tg_user(cb), game_id, tid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.matches_menu())
        return
    await show(cb, "✅ Результат обновлён.\n\n" + _series_text(v, svc.cfg.timezone), akb.series_card(v.match))
