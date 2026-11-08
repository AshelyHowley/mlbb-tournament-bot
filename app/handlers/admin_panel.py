from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton as B, InlineKeyboardMarkup

from app.bot.context import Services
from app.services.bracket import render_bracket
from app.handlers.common import IsStaff, show, tg_user
from app.keyboards import admin as akb
from app.services.errors import ServiceError
from app.services.tournament import CONFIRM_WORD
from app.utils.text import esc, fmt_dt, parse_int, person

router = Router(name="admin_panel")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

ADMIN_TITLE = "🛠 <b>Админ-панель</b>"


class FinishTournament(StatesGroup):
    typing = State()      # waiting for the operator to type the confirmation word
    confirmed = State()   # word typed correctly - the final button is now live


async def _menu(cb_or_msg, svc: Services, state: FSMContext) -> None:
    is_main = svc.cfg.is_bot_admin(cb_or_msg.from_user.id)
    await state.clear()
    await show(cb_or_msg, ADMIN_TITLE, akb.admin_menu(is_main))


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, svc: Services) -> None:
    await _menu(message, svc, state)


@router.callback_query(F.data == "adm")
async def cb_admin(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await _menu(cb, svc, state)


# ------------------------------------------------------------------ teams
@router.callback_query(F.data.regexp(r"^adm:t:\d{1,6}$"))
async def teams_list(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    total, page, last, rows = await svc.admin.teams_page(int(cb.data.split(":")[2]))
    text = f"👥 <b>Команды</b>\nВсего: <b>{total}</b>" + ("" if total else "\n\nПока нет ни одной команды.")
    await show(cb, text, akb.teams_list(rows, page, last))


@router.callback_query(F.data.regexp(r"^adm:tc:\d{1,18}$"))
async def team_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    try:
        team, members = await svc.admin.team_card(tid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    who = [person(m['full_name'], m['username'], m['user_id']) for m in members]  # username is optional
    cap = next((w for m, w in zip(members, who) if m['is_captain']), 'ожидается')
    ready = "✅ Команда готова" if len(members) >= 2 else "⚠️ Не готова к участию (нужно минимум 2 игрока)"
    lines = [f"👥 <b>{esc(team['name'])}</b>", f"Капитан: {cap}",
             f"Регистратор: {person(team['creator_name'], team['creator_username'], team['created_by'])}",
             f"Состав: {len(members)}/5 — {ready}"]
    lines += [f"  {i}. {'👑 ' if m['is_captain'] else ''}{w}" for i, (m, w) in enumerate(zip(members, who), 1)]
    lines.append(f"Дата регистрации: {fmt_dt(team['created_at'], svc.cfg.timezone)}")
    await show(cb, "\n".join(lines), akb.team_card(tid))


@router.callback_query(F.data.regexp(r"^adm:td:\d{1,18}$"))
async def team_delete_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    try:
        team, _ = await svc.admin.team_card(tid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    await show(
        cb,
        f"⚠️ Снять команду <b>{esc(team['name'])}</b> с регистрации? Все участники будут уведомлены.",
        akb.confirm_delete(f"adm:tdok:{tid}", f"adm:tc:{tid}"),
    )


@router.callback_query(F.data.regexp(r"^adm:tdok:\d{1,18}$"))
async def team_delete(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    tid = parse_int(cb.data.split(":")[2])
    try:
        team, members = await svc.admin.team_card(tid)
        # Notify all members including captain
        note = f"ℹ️ Администратор снял команду «{team['name']}» с регистрации. Вы больше не состоите в команде."
        for m in members:
            try:
                await svc.bot.send_message(m["user_id"], note)
            except Exception:
                pass
        await svc.admin.delete_team(tid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    await show(cb, "✅ Команда снята с регистрации.", akb.to_admin())


# ------------------------------------------------------------------ solo
@router.callback_query(F.data.regexp(r"^adm:s:\d{1,6}$"))
async def solo_list(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    total, page, last, rows = await svc.admin.solo_page(int(cb.data.split(":")[2]))
    text = f"👤 <b>Соло-игроки</b>\nВсего: <b>{total}</b>" + ("" if total else "\n\nПока никого нет.")
    await show(cb, text, akb.solo_list(rows, page, last))


@router.callback_query(F.data.regexp(r"^adm:sc:\d{1,18}$"))
async def solo_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = parse_int(cb.data.split(":")[2])
    try:
        row = await svc.admin.solo_card(uid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    await show(
        cb,
        f"👤 <b>Соло-игрок</b>\nTelegram: {person(row['full_name'], row['username'], row['user_id'])}\nРоль: <b>{esc(row['role'])}</b>\n"
        f"Дата регистрации: {fmt_dt(row['created_at'], svc.cfg.timezone)}",
        akb.solo_card(uid),
    )


@router.callback_query(F.data.regexp(r"^adm:sd:\d{1,18}$"))
async def solo_delete_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = parse_int(cb.data.split(":")[2])
    try:
        row = await svc.admin.solo_card(uid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    await show(
        cb,
        f"⚠️ Снять соло-игрока {person(row['full_name'], row['username'], row['user_id'])} с регистрации? Игрок будет уведомлён.",
        akb.confirm_delete(f"adm:sdok:{uid}", f"adm:sc:{uid}"),
    )


@router.callback_query(F.data.regexp(r"^adm:sdok:\d{1,18}$"))
async def solo_delete(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = parse_int(cb.data.split(":")[2])
    try:
        row = await svc.admin.solo_card(uid)
        try:
            await svc.bot.send_message(uid, "ℹ️ Администратор снял вас с соло-регистрации.")
        except Exception:
            pass
        await svc.admin.delete_solo(uid)
    except ServiceError as e:
        await show(cb, e.user_message, akb.to_admin())
        return
    await show(cb, "✅ Соло-игрок снят с регистрации.", akb.to_admin())


# ------------------------------------------------------------------ registration switch
@router.callback_query(F.data == "adm:reg")
async def reg_status(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    open_ = await svc.reg.is_open()
    await show(cb, f"🔐 <b>Регистрация</b>\nСейчас: {'🔓 открыта' if open_ else '🔒 закрыта'}", akb.registration(open_))


@router.callback_query(F.data.in_({"adm:reg:set:0", "adm:reg:set:1"}))
async def reg_set(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    value = cb.data.endswith(":1")
    await svc.reg.set_open(value)  # explicit value -> idempotent on double click
    await show(cb, f"🔐 <b>Регистрация</b>\nСейчас: {'🔓 открыта' if value else '🔒 закрыта'}", akb.registration(value))


# ------------------------------------------------------------------ finish tournament (archive)
def _placements_text(p) -> str:
    return (
        f"👑 1 место: {esc(p.first_name) if p.first_name else 'не определено'}\n"
        f"🥈 2 место: {esc(p.second_name) if p.second_name else 'не определено'}\n"
        f"🥉 3 место: {esc(p.third_name) if p.third_name else 'не определено'}"
    )


@router.callback_query(F.data == "tf:start")
async def finish_start(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await state.clear()
    p = await svc.tournaments.placements()
    if p.ready:
        await show(cb, f"⚠️ <b>Завершение турнира</b>\n\n{_placements_text(p)}\n\nВсе призовые места определены.", akb.finish_confirm(True))
    else:
        await show(
            cb,
            f"⚠️ <b>ТУРНИР ЕЩЁ НЕ ЗАВЕРШЁН</b>\n\nНе все призовые места определены.\n\n{_placements_text(p)}",
            akb.finish_confirm(False),
        )


@router.callback_query(F.data.in_({"tf:go", "tf:early"}))
async def finish_confirm2(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    p = await svc.tournaments.placements()
    await show(
        cb,
        "⚠️ <b>ЗАВЕРШЕНИЕ ТУРНИРА</b>\n\nТекущий турнир будет завершён. Все данные будут сохранены в 📚 История турниров. "
        "После этого будет создан новый чистый турнир.\n\nСохранятся: команды и составы, соло, сетка, серии, катки, "
        "1/2/3 места и статистика.\n\n" + _placements_text(p) + f"\n\nДля подтверждения введите слово: <b>{CONFIRM_WORD}</b>",
        akb.to_admin(),
    )
    await state.set_state(FinishTournament.typing)
    await state.update_data(force=cb.data == "tf:early")


@router.message(FinishTournament.typing, F.text)
async def finish_word(message: Message, state: FSMContext) -> None:
    if message.text.strip() != CONFIRM_WORD:
        await message.answer(f"❌ Введите точно: {CONFIRM_WORD}", reply_markup=akb.to_admin())
        return
    await state.set_state(FinishTournament.confirmed)
    await message.answer("Подтверждение получено. Нажмите кнопку для окончательного завершения турнира.", reply_markup=akb.finish_final())


@router.message(FinishTournament.typing, ~F.text)
async def finish_word_wrong_type(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте текстом слово подтверждения.")


@router.callback_query(FinishTournament.confirmed, F.data == "tf:ok")
async def finish_ok(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    try:
        tid, p = await svc.tournaments.finalize(tg_user(cb), force=bool(data.get("force")))
    except ServiceError as e:
        # covers TournamentNotReady (podium incomplete) and the rare NotFound race where another
        # staff member's concurrent finalize already archived this exact tournament first.
        await show(cb, e.user_message, akb.to_admin())
        return
    await show(
        cb,
        "✅ <b>ТУРНИР ЗАВЕРШЁН</b>\n\nРезультаты сохранены в 📚 История турниров.\n\n" + _placements_text(p) +
        "\n\nСоздан новый турнир.\n👥 Команд: 0\n👤 Соло: 0\n🎮 Матчей: 0\n🏆 Сетка: пустая",
        akb.finish_done(),
    )
    if p.ready:  # a fair final-results send only makes sense when the podium is real
        await cb.bot.send_message(
            cb.from_user.id,
            "📢 <b>ИТОГИ ТУРНИРА</b>\n\n" + _placements_text(p) + "\n\nКому отправить?",
            reply_markup=akb.sn_audience_final(tid),
        )


@router.callback_query(F.data == "tf:ok")
async def finish_ok_stale(cb: CallbackQuery) -> None:
    await cb.answer("Действие устарело, начните заново.", show_alert=True)


@router.callback_query(F.data == "adm:bracket")
async def admin_bracket(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    pages = render_bracket(await svc.admin.bracket_rows())
    from aiogram.types import InlineKeyboardMarkup
    rows = [
        [B(text="🏆 Создать сетку", callback_data="br:random"), B(text="✏️ Редактировать", callback_data="br:edit")],
        [B(text="◀️ Админ-панель", callback_data="adm")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await show(cb, pages[0], markup if len(pages) == 1 else None)
    for i, extra in enumerate(pages[1:], 1):
        await cb.bot.send_message(cb.from_user.id, extra, reply_markup=markup if i == len(pages) - 1 else None)


@router.callback_query(F.data.regexp(r"^qb:bracket:(all|captains)$"))
async def quick_bracket(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer("Отправляю...")
    audience = cb.data.split(":")[2]
    pages = render_bracket(await svc.admin.bracket_rows())
    text = "\n".join(pages)
    async with svc.team.pool.acquire() as conn:
        from app.repositories.broadcasts import recipients
        ids = await recipients(conn, audience)
    sent = 0
    for uid in ids:
        try:
            await svc.bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
    await show(cb, f"✅ Сетка отправлена: {sent} из {len(ids)}", akb.to_admin())


@router.callback_query(F.data == "rnd:solo")
async def randomize_solo(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer("Собираю команды...")
    import random
    from app.repositories.teams import insert_team, add_member
    from app.utils.text import new_invite_token
    async with svc.team.pool.acquire() as conn:
        async with conn.transaction():
            solos = await conn.fetch(
                "SELECT s.user_id, s.username, s.role, u.full_name "
                "FROM solo_players s JOIN users u ON u.telegram_id = s.user_id "
                "WHERE s.tournament_id = (SELECT id FROM tournaments WHERE status = 'active')"
            )
            if len(solos) < 5:
                await show(cb, f"ℹ️ Соло-игроков: {len(solos)}. Нужно минимум 5.", akb.to_admin())
                return
            random.shuffle(solos)
            created = 0
            while len(solos) >= 5:
                group = [solos.pop() for _ in range(5)]
                captain = group[0]
                name = f"Team {created + 1}"
                name_norm = name.lower().replace(" ", "")
                tid = await insert_team(
                    conn, name, name_norm, captain["user_id"], captain["user_id"],
                    new_invite_token(), None,
                )
                for i, p in enumerate(group):
                    await add_member(conn, tid, p["user_id"], is_captain=(i == 0))
                    await conn.execute("DELETE FROM solo_players WHERE user_id = $1", p["user_id"])
                created += 1
            left = len(solos)
    await show(cb, f"✅ Создано команд: <b>{created}</b>\nОсталось соло: <b>{left}</b>", akb.to_admin())


@router.callback_query(F.data == "br:new")
async def bracket_new(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        teams = await conn.fetch(
            "SELECT id, name FROM teams WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
            "AND EXISTS (SELECT 1 FROM team_members m WHERE m.team_id = teams.id) ORDER BY name"
        )
    if len(teams) < 2:
        await show(cb, "ℹ️ Нужно минимум 2 команды для сетки.", akb.to_admin())
        return
    rows = [[B(text="🎲 Случайно распределить все", callback_data="br:random")]]
    rows += [[B(text=t["name"][:30], callback_data=f"br:pick:{t['id']}")] for t in teams]
    rows.append([B(text="◀️ Назад", callback_data="adm:bracket")])
    from aiogram.types import InlineKeyboardMarkup
    await show(cb, "🏆 <b>Создание сетки</b>\nВыберите первую команду или случайное распределение:", InlineKeyboardMarkup(inline_keyboard=rows))


_last_bracket_msg: dict[int, int] = {}

async def _send_bracket(bot, chat_id, png, caption):
    from aiogram.types import BufferedInputFile
    old = _last_bracket_msg.get(chat_id)
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except Exception:
            pass
    msg = await bot.send_document(chat_id, BufferedInputFile(png, filename="bracket.png"), caption=caption)
    _last_bracket_msg[chat_id] = msg.message_id

@router.callback_query(F.data == "br:random")
async def bracket_random(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer("Распределяю...")
    import random
    async with svc.team.pool.acquire() as conn:
        async with conn.transaction():
            teams = await conn.fetch(
                "SELECT id, name FROM teams WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
                "AND EXISTS (SELECT 1 FROM team_members m WHERE m.team_id = teams.id) "
                "AND name_norm NOT IN ('tbd_a','tbd_b','tbd_placeholder')"
            )
            if len(teams) < 2:
                await show(cb, "ℹ️ Нужно минимум 2 команды.", akb.to_admin())
                return
            await conn.execute("DELETE FROM matches WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active')")
            ids = [t["id"] for t in teams]
            random.shuffle(ids)
            n = len(ids)
            # Target: 16 for main bracket
            main = 16 if n > 8 else (8 if n > 4 else 4)
            extra = max(0, n - main)
            # Play-in
            if extra > 0:
                play = ids[:extra * 2]
                rest = ids[extra * 2:]
                for i in range(0, len(play), 2):
                    await conn.execute(
                        "INSERT INTO matches (stage_code, team_a_id, team_b_id, tournament_id) "
                        "VALUES ('playin', $1, $2, (SELECT id FROM tournaments WHERE status = 'active'))",
                        play[i], play[i+1],
                    )
            else:
                rest = ids[:]
            # Stages without semi: r16/r8 -> r4 -> final -> finalist
            if main == 16:
                stages = [("r16", 8), ("r8", 4), ("r4", 2), ("final", 1)]
            elif main == 8:
                stages = [("r8", 4), ("r4", 2), ("final", 1)]
            else:
                stages = [("r4", 2), ("final", 1)]
            first_code, first_cnt = stages[0]
            slots = [None] * extra + rest
            for i in range(0, main, 2):
                a = slots[i] if i < len(slots) else None
                b = slots[i+1] if i+1 < len(slots) else None
                await conn.execute(
                    "INSERT INTO matches (stage_code, team_a_id, team_b_id, tournament_id) "
                    "VALUES ($1, $2, $3, (SELECT id FROM tournaments WHERE status = 'active'))",
                    first_code, a, b,
                )
            for code, cnt in stages[1:]:
                for _ in range(cnt):
                    await conn.execute(
                        "INSERT INTO matches (stage_code, team_a_id, team_b_id, tournament_id) "
                        "VALUES ($1, NULL, NULL, (SELECT id FROM tournaments WHERE status = 'active'))",
                        code,
                    )
            # Finalist slot
            await conn.execute(
                "INSERT INTO matches (stage_code, team_a_id, team_b_id, tournament_id) "
                "VALUES ('finalist', NULL, NULL, (SELECT id FROM tournaments WHERE status = 'active'))"
            )
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COALESCE(s.title, m.stage_code) AS stage, ta.name AS team_a, tb.name AS team_b, tw.name AS winner "
            "FROM matches m LEFT JOIN stages s ON s.code = m.stage_code "
            "LEFT JOIN teams ta ON ta.id = m.team_a_id "
            "LEFT JOIN teams tb ON tb.id = m.team_b_id "
            "LEFT JOIN teams tw ON tw.id = m.winner_id "
            "WHERE m.tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
            "ORDER BY CASE m.stage_code WHEN 'playin' THEN 0 WHEN 'r16' THEN 1 WHEN 'r8' THEN 2 WHEN 'r4' THEN 3 WHEN 'final' THEN 4 WHEN 'finalist' THEN 5 ELSE 9 END, m.id"
        )
    from app.services.bracket_image import render_bracket_image
    png = render_bracket_image([dict(r) for r in rows])
    if png:
        await _send_bracket(cb.bot, cb.from_user.id, png, "🏆 Сетка")
    await show(cb, f"✅ Создано: {n} команд", akb.to_admin())


@router.callback_query(F.data.regexp(r"^br:pick:\d+$"))
async def bracket_pick(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    tid = int(cb.data.split(":")[2])
    data = await state.get_data()
    first = data.get("br_first")
    if first is None:
        await state.update_data(br_first=tid)
        async with svc.team.pool.acquire() as conn:
            teams = await conn.fetch(
                "SELECT id, name FROM teams WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
                "AND id != $1 AND EXISTS (SELECT 1 FROM team_members m WHERE m.team_id = teams.id) ORDER BY name",
                tid,
            )
        rows = [[B(text=t["name"][:30], callback_data=f"br:pick:{t['id']}")] for t in teams]
        rows.append([B(text="❌ Отмена", callback_data="br:new")])
        from aiogram.types import InlineKeyboardMarkup
        await show(cb, "Выберите вторую команду:", InlineKeyboardMarkup(inline_keyboard=rows))
        return
    # Second team selected — ask stage
    await state.update_data(br_second=tid)
    stages = [("r16", "1/16"), ("r8", "1/8"), ("r4", "1/4"), ("semi", "Полуфинал"), ("final", "Финал")]
    rows = [[B(text=title, callback_data=f"br:stage:{code}")] for code, title in stages]
    rows.append([B(text="❌ Отмена", callback_data="br:new")])
    from aiogram.types import InlineKeyboardMarkup
    await show(cb, "Выберите стадию:", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^br:stage:[a-z0-9]+$"))
async def bracket_stage(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    stage = cb.data.split(":")[2]
    data = await state.get_data()
    a, b = data.get("br_first"), data.get("br_second")
    await state.clear()
    if not a or not b:
        await show(cb, "⚠️ Сессия сброшена. Начните заново.", akb.to_admin())
        return
    async with svc.team.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO matches (stage_code, team_a_id, team_b_id, tournament_id) "
            "VALUES ($1, $2, $3, (SELECT id FROM tournaments WHERE status = 'active'))",
            stage, a, b,
        )
    await show(cb, "✅ Пара добавлена в сетку.", akb.to_admin())


@router.callback_query(F.data == "br:edit")
async def bracket_edit(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.id, m.status, m.winner_id, s.title, "
            "ta.name AS a, ta.id AS aid, tb.name AS b, tb.id AS bid "
            "FROM matches m JOIN stages s ON s.code = m.stage_code "
            "JOIN teams ta ON ta.id = m.team_a_id JOIN teams tb ON tb.id = m.team_b_id "
            "WHERE m.tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
            "ORDER BY s.sort_order, m.id"
        )
    if not rows:
        await show(cb, "ℹ️ Сетка пуста.", akb.to_admin())
        return
    lines = ["✏️ <b>Редактирование сетки</b>\n"]
    kb_rows = []
    for r in rows:
        win = ""
        if r["status"] == "finished":
            win = " ✅"
        lines.append(f"{r['title']}: {r['a']} vs {r['b']}{win}")
        kb_rows.append([
            B(text=f"🏆 {r['a'][:12]}", callback_data=f"br:win:{r['id']}:{r['aid']}"),
            B(text=f"🏆 {r['b'][:12]}", callback_data=f"br:win:{r['id']}:{r['bid']}"),
        ])
        kb_rows.append([B(text=f"🗑 Удалить пару", callback_data=f"br:del:{r['id']}")])
    kb_rows.append([B(text="🔄 Обновить картинку", callback_data="br:refresh")])
    kb_rows.append([B(text="🗑 Удалить все", callback_data="br:clear")])
    kb_rows.append([B(text="◀️ Назад", callback_data="adm:bracket")])
    from aiogram.types import InlineKeyboardMarkup
    await show(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.regexp(r"^br:win:\d+:\d+$"))
async def bracket_win(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    _, _, mid, wid = cb.data.split(":")
    mid, wid = int(mid), int(wid)
    next_stage = {"playin": "r8", "r16": "r8", "r8": "r4", "r4": "semi", "semi": "final"}
    async with svc.team.pool.acquire() as conn:
        async with conn.transaction():
            m = await conn.fetchrow(
                "SELECT stage_code, team_a_id, team_b_id FROM matches WHERE id = $1", mid
            )
            if not m:
                await show(cb, "⚠️ Матч не найден.", akb.to_admin())
                return
            await conn.execute(
                "UPDATE matches SET winner_id = $2, status = 'finished', finished_at = datetime('now') WHERE id = $1",
                mid, wid,
            )
            nxt = next_stage.get(m["stage_code"])
            if nxt:
                tbd = await conn.fetchval("SELECT id FROM teams WHERE name_norm = 'tbd_placeholder' LIMIT 1")
                # Position: which match number in current stage
                pos = await conn.fetchval(
                    "SELECT COUNT(*) FROM matches WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
                    "AND stage_code = $1 AND id <= $2",
                    m["stage_code"], mid,
                )
                # Next match index (0-based): pos//2
                next_matches = await conn.fetch(
                    "SELECT id, team_a_id, team_b_id FROM matches "
                    "WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active') AND stage_code = $1 "
                    "ORDER BY id",
                    nxt,
                )
                if next_matches:
                    # Find first match with an empty slot
                    placed = False
                    for nm in next_matches:
                        if nm["team_a_id"] is None:
                            await conn.execute("UPDATE matches SET team_a_id = $2 WHERE id = $1", nm["id"], wid)
                            placed = True
                            break
                        elif nm["team_b_id"] is None:
                            await conn.execute("UPDATE matches SET team_b_id = $2 WHERE id = $1", nm["id"], wid)
                            placed = True
                            break
                    if not placed:
                        # All full — skip (shouldn't happen in correct bracket)
                        pass
    # Regenerate bracket image
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT s.title AS stage, ta.name AS team_a, tb.name AS team_b, tw.name AS winner "
            "FROM matches m JOIN stages s ON s.code = m.stage_code "
            "LEFT JOIN teams ta ON ta.id = m.team_a_id "
            "LEFT JOIN teams tb ON tb.id = m.team_b_id "
            "LEFT JOIN teams tw ON tw.id = m.winner_id "
            "WHERE m.tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
            "ORDER BY s.sort_order, m.id"
        )
    from app.services.bracket_image import render_bracket_image
    png = render_bracket_image([dict(r) for r in rows])
    if png:
        from aiogram.types import BufferedInputFile
        await _send_bracket(cb.bot, cb.from_user.id, png, "✅ Победитель вышел")
    # Stay in edit menu
    await bracket_edit(cb, svc)


@router.callback_query(F.data.regexp(r"^br:del:\d+$"))
async def bracket_del(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    mid = int(cb.data.split(":")[2])
    async with svc.team.pool.acquire() as conn:
        await conn.execute("DELETE FROM matches WHERE id = $1", mid)
    await bracket_edit(cb, svc)


@router.callback_query(F.data == "br:export")
async def bracket_export(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    pages = render_bracket(await svc.admin.bracket_rows())
    text = "\n".join(pages)
    # Strip HTML tags for plain text file
    import re
    plain = re.sub(r"<[^>]+>", "", text)
    from aiogram.types import BufferedInputFile
    data = plain.encode("utf-8")
    await cb.bot.send_document(
        cb.from_user.id,
        BufferedInputFile(data, filename="bracket.txt"),
        caption="🏆 Турнирная сетка",
    )
    await show(cb, "✅ Файл отправлен.", akb.to_admin())


@router.callback_query(F.data == "log:reg")
async def log_registrations(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, action, team_name, details, created_at FROM registration_log "
            "ORDER BY id DESC LIMIT 30"
        )
    if not rows:
        await show(cb, "📋 <b>История регистраций</b>\n\nПока пусто.", akb.to_admin())
        return
    lines = ["📋 <b>История регистраций</b> (последние 30)\n"]
    for r in rows:
        lines.append(f"{r['created_at'][:16]} | {r['action']} | {r['team_name'] or '—'} | id:{r['user_id']}")
        if r['details']:
            lines.append(f"  {r['details']}")
    await show(cb, "\n".join(lines), akb.to_admin())


@router.callback_query(F.data == "log:act")
async def log_actions(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, username, action, details, created_at FROM action_log "
            "ORDER BY id DESC LIMIT 40"
        )
    if not rows:
        await show(cb, "📜 <b>Логи действий</b>\n\nПока пусто.", akb.to_admin())
        return
    lines = ["📜 <b>Логи действий</b> (последние 40)\n"]
    for r in rows:
        uname = f"@{r['username']}" if r['username'] else f"id:{r['user_id']}"
        lines.append(f"{r['created_at'][:16]} | {uname} | {r['action']}")
    await show(cb, "\n".join(lines), akb.to_admin())


@router.callback_query(F.data == "br:clear")
async def bracket_clear(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM matches WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active')"
        )
    await show(cb, "✅ Сетка очищена.", akb.to_admin())


@router.callback_query(F.data == "br:refresh")
async def bracket_refresh(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer("Обновляю...")
    async with svc.team.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COALESCE(s.title, m.stage_code) AS stage, ta.name AS team_a, tb.name AS team_b, tw.name AS winner "
            "FROM matches m LEFT JOIN stages s ON s.code = m.stage_code "
            "LEFT JOIN teams ta ON ta.id = m.team_a_id "
            "LEFT JOIN teams tb ON tb.id = m.team_b_id "
            "LEFT JOIN teams tw ON tw.id = m.winner_id "
            "WHERE m.tournament_id = (SELECT id FROM tournaments WHERE status = 'active') "
            "ORDER BY CASE m.stage_code WHEN 'playin' THEN 0 WHEN 'r16' THEN 1 WHEN 'r8' THEN 2 WHEN 'r4' THEN 3 WHEN 'semi' THEN 4 WHEN 'final' THEN 5 ELSE 9 END, m.id"
        )
    from app.services.bracket_image import render_bracket_image
    png = render_bracket_image([dict(r) for r in rows])
    if png:
        from aiogram.types import BufferedInputFile
        await _send_bracket(cb.bot, cb.from_user.id, png, "🏆 Текущая сетка")
    await bracket_edit(cb, svc)

@router.callback_query(F.data == "adm:tdelall")
async def teams_delete_all(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    async with svc.team.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM matches WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active')")
            await conn.execute("DELETE FROM team_members WHERE team_id IN (SELECT id FROM teams WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active'))")
            await conn.execute("DELETE FROM teams WHERE tournament_id = (SELECT id FROM tournaments WHERE status = 'active')")
    await show(cb, "✅ Все команды удалены.", akb.to_admin())

