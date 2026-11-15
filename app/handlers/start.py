from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.context import Services
from app.keyboards import common as kbs
from app.handlers.common import format_solo, format_team, menu_view, show
from app.keyboards.common import back_to_menu, my_reg_menu
from app.handlers.team import card_markup
from app.services.bracket import render_bracket
from app.utils.audit import log_registration
from app.services.bracket_png import active_bracket_png

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

_last_public_bracket: dict[int, int] = {}  # user id -> message id последней присланной картинки


@router.callback_query(F.data == "bracket")
async def cb_bracket(cb: CallbackQuery, svc: Services) -> None:
    try:
        await cb.answer()
    except TelegramBadRequest:
        pass  # устаревшее нажатие - сетку всё равно показываем
    pages = render_bracket(await svc.admin.bracket_rows())
    png = await active_bracket_png(svc.team.pool)
    # кнопка «назад» - под последним сообщением (под картинкой, если она есть)
    await show(cb, pages[0], back_to_menu() if len(pages) == 1 and png is None else None)
    for i, extra in enumerate(pages[1:], 1):
        last = i == len(pages) - 1 and png is None
        await cb.bot.send_message(cb.from_user.id, extra, reply_markup=back_to_menu() if last else None)
    if png is None:
        return
    uid = cb.from_user.id
    old = _last_public_bracket.pop(uid, None)
    if old:  # не копим в чате старые картинки - присылаем свежую вместо прежней
        try:
            await cb.bot.delete_message(uid, old)
        except TelegramBadRequest:
            pass
    msg = await cb.bot.send_document(
        uid, BufferedInputFile(png, filename="bracket.png"),
        caption="🏆 Турнирная сетка", reply_markup=back_to_menu(),
    )
    _last_public_bracket[uid] = msg.message_id


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
                # same order as AdminService.delete_team: unlink bracket references, drop the matches,
                # then the team (members cascade; the captain row cannot be deleted on its own)
                await conn.execute(
                    "UPDATE matches SET next_match_id = NULL WHERE next_match_id IN "
                    "(SELECT id FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1)",
                    row["id"],
                )
                await conn.execute("DELETE FROM matches WHERE team_a_id = $1 OR team_b_id = $1 OR winner_id = $1", row["id"])
                await log_registration(conn, uid, 'unregister_team', row["name"], team_id=row["id"],
                                       member_role="captain")
                await conn.execute("DELETE FROM teams WHERE id = $1", row["id"])
            # Solo case: remove solo registration
            if await conn.execute(
                "DELETE FROM solo_players WHERE user_id = $1 AND tournament_id = "
                "(SELECT id FROM tournaments WHERE status = 'active')",
                uid,
            ) == "DELETE 1":
                await log_registration(conn, uid, 'unregister_solo')
    if team_name and member_ids:
        note = f"ℹ️ Капитан снял команду «{team_name}» с регистрации. Вы больше не состоите в команде."
        for mid in member_ids:
            try:
                await svc.bot.send_message(mid, note)
            except Exception:
                pass
    text, markup = await menu_view(svc, uid)
    await show(cb, "✅ Вы сняты с регистрации.\n\n" + text, markup)

HOF_TEXT = """〰️Чат Mobile Legends/ Tournament Chat 〰️

❤️‍🔥<b>Зал Славы</b>❤️‍🔥

🤩<b>10.2025</b> 🍁
🌎Турнир "Первый"
🦿Тип боёв "смешанный"
🏆Приз 5000р
🏆Победитель @Stepanova7

🦌<b>11.2025</b>🦌
🌎Турнир "Смежный"
🦿Тип боёв "5*5"
🏆Приз 5000 алмазов
🏆Победитель Команда нашего чата, капитан @Kim4D

👋<b>12.2025</b>👋
🌎Турнир "Новогодний Хаос"
🦿Тип боёв "смешанный"
🏆Приз 6500р
🏆Победитель @Stepanova7

🪙 <b>02.2026</b> 🪙
🌎Турнир "Зимний"
🦿Тип боёв "2*2"
🏆Приз 3000р
🏆Победитель "Мы Едим Лошадей"

🌸<b>05.2026</b>🌸
🌎Турнир "Железный impakt"
🦿Тип боёв "5*5"
🏆Приз 5000р
🏆Победитель "Krasavchiki"

🌞<b>06.2026</b>🌞
🌎Турнир "Battle Fury"
🦿Тип боёв "5*5"
🏆Приз 6000р
🏆Победитель "Natus amittere"

🌞<b>07.2026</b>🌞
🌎Турнир "Кубок Огня"
🦿Тип боёв "5*5"
🏆Приз 6500р
🏆Победитель "Dobeg squad"

🌞<b>09.2026</b>🌞
🌎Турнир "Sun Straike"
🦿Тип боёв "5*5"
🏆Приз 6000р
🏆Победитель "Rakuzan"

🌞<b>10.2026</b>🌞
🌎Турнир "Sunset"
🦿Тип боёв "5*5"
🏆Приз 6000р
🏆Победитель "APBE"
"""

@router.callback_query(F.data == "hof")
async def hall_of_fame(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    await show(cb, HOF_TEXT, kbs.back_to_menu())

