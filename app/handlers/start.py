from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.context import Services
from app.handlers.common import format_solo, format_team, menu_view, show
from app.keyboards.common import back_to_menu, my_reg_menu
from app.handlers.team import card_markup
from app.services.bracket import render_bracket

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, svc: Services) -> None:
    await state.clear()
    text, markup = await menu_view(svc, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, svc: Services) -> None:
    await state.clear()
    text, markup = await menu_view(svc, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await state.clear()
    text, markup = await menu_view(svc, cb.from_user.id)
    await show(cb, text, markup)


@router.callback_query(F.data == "me")
async def cb_me(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = cb.from_user.id
    reg = await svc.reg.my_registration(uid)
    if reg is None:
        text, markup = await menu_view(svc, uid)
        await show(cb, "Вы пока не зарегистрированы.\n\n" + text, markup)
        return
    parts = []
    view = await svc.team.view_for_user(uid) if reg.has_team else None
    if view is not None:
        parts.append(format_team(view, uid, title="👥 <b>Ваша регистрация: команда</b>"))
    if reg.solo:
        parts.append(format_solo(reg, uid))
    # Unregister button only for captain
    can_unreg = (view is not None and view.captain_id == uid) or reg.solo
    markup = my_reg_menu(can_unreg)
    await show(cb, "\n\n".join(parts) or "Вы пока не зарегистрированы.", markup)

@router.callback_query(F.data == "bracket")
async def cb_bracket(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    pages = render_bracket(await svc.admin.bracket_rows())
    await show(cb, pages[0], back_to_menu() if len(pages) == 1 else None)
    for i, extra in enumerate(pages[1:], 1):
        await cb.bot.send_message(cb.from_user.id, extra, reply_markup=back_to_menu() if i == len(pages) - 1 else None)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "me:unregister")
async def cb_unregister(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = cb.from_user.id
    team_name = None
    member_ids: list[int] = []
    async with svc.team.pool.acquire() as conn:
        async with conn.transaction():
            # Team case: captain disbands the whole team
            row = await conn.fetchrow(
                "SELECT t.id, t.name FROM teams t "
                "JOIN team_members m ON m.team_id = t.id "
                "WHERE m.user_id = $1 AND m.is_captain = 1 AND t.tournament_id = "
                "(SELECT id FROM tournaments WHERE status = 'active')",
                uid,
            )
            if row:
                team_name = row["name"]
                members = await conn.fetch("SELECT user_id FROM team_members WHERE team_id = $1", row["id"])
                member_ids = [m["user_id"] for m in members if m["user_id"] != uid]
                await conn.execute("DELETE FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1", row["id"])
                await conn.execute("DELETE FROM team_members WHERE team_id = $1", row["id"])
                await conn.execute("DELETE FROM teams WHERE id = $1", row["id"])
            # Solo case: remove solo registration
            await conn.execute(
                "DELETE FROM solo_players WHERE user_id = $1 AND tournament_id = "
                "(SELECT id FROM tournaments WHERE status = 'active')",
                uid,
            )
    if team_name and member_ids:
        note = f"ℹ️ Капитан снял команду «{team_name}» с регистрации. Вы больше не состоите в команде."
        for mid in member_ids:
            try:
                await svc.bot.send_message(mid, note)
            except Exception:
                pass
    text, markup = await menu_view(svc, uid)
    await show(cb, "✅ Вы сняты с регистрации.\n\n" + text, markup)
