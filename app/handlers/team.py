"""Team invites (player + captain deep links), join/accept, registrar tools and roster management.
Every action is re-authorised server-side; callback_data never carries authority."""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.context import Services
from app.handlers.common import format_team, menu_view, show, tg_user
from app.keyboards import admin as akb
from app.keyboards import common as ckb
from app.keyboards import team as tkb
from app.services.errors import (
    AlreadyInOtherTeam,
    CaptainExists,
    CaptainRequired,
    DeputyInviteClaimed,
    InvalidInvite,
    NotCaptain,
    NotFound,
    RegistrationClosed,
    ServiceError,
    TeamFull,
)
from app.services.team import MAX_TEAM_SIZE, InvitePeek, TeamView
from app.utils.text import esc, parse_captain_payload, parse_deputy_payload, parse_int, parse_invite_payload, person

log = logging.getLogger(__name__)
router = Router(name="team")

_TOKEN = r"[A-Za-z0-9_-]{32,48}"
SOLO_MSG = "ℹ️ Вы зарегистрированы как соло-игрок — вступить в команду нельзя."
ALREADY_CAPTAIN_MSG = "✅ Вы уже капитан этой команды."


async def _notify(svc: Services, chat_id: int, text: str) -> None:
    """Best-effort notification; a blocked/unreachable user must never break the main action."""
    try:
        await svc.bot.send_message(chat_id, text)
    except TelegramAPIError:
        log.info("notification to %s not delivered", chat_id)


def _player_status_message(peek: InvitePeek) -> Optional[tuple[str, object]]:
    """Text + keyboard for every non-joinable player-invite state; None when the user may join."""
    return {
        "invalid": (InvalidInvite().user_message, ckb.back_to_menu()),
        "mine": ("✅ Вы уже состоите в этой команде.", tkb.my_team_only()),
        "other": (AlreadyInOtherTeam().user_message, tkb.my_team_only()),
        "solo": (SOLO_MSG, ckb.back_to_menu()),
        "no_captain": (CaptainRequired().user_message, ckb.back_to_menu()),
        "closed": (RegistrationClosed().user_message, ckb.back_to_menu()),
        "full": (TeamFull().user_message, ckb.back_to_menu()),
    }.get(peek.status)


def _captain_status_message(peek: InvitePeek) -> Optional[tuple[str, object]]:
    return {
        "invalid": ("❌ Приглашение больше недействительно.", ckb.back_to_menu()),
        "captain_exists": (CaptainExists().user_message, ckb.back_to_menu()),
        "mine_captain": (ALREADY_CAPTAIN_MSG, tkb.my_team_only()),
        "other": (AlreadyInOtherTeam().user_message, tkb.my_team_only()),
        "solo": ("ℹ️ Вы зарегистрированы как соло-игрок — стать капитаном команды нельзя.", ckb.back_to_menu()),
        "closed": (RegistrationClosed().user_message, ckb.back_to_menu()),
        "full": (TeamFull().user_message, ckb.back_to_menu()),
    }.get(peek.status)


# ------------------------------------------------------------------ deep links
@router.message(CommandStart(deep_link=True))
async def start_with_payload(message: Message, command: CommandObject, state: FSMContext, svc: Services) -> None:
    await state.clear()
    uid = message.from_user.id
    player = parse_invite_payload(command.args)
    captain = parse_captain_payload(command.args)
    deputy = parse_deputy_payload(command.args)
    if player is None and captain is None and deputy is None:  # unknown/malformed payload: plain /start, reveal nothing
        text, markup = await menu_view(svc, uid)
        await message.answer(text, reply_markup=markup)
        return
    if deputy is not None:
        peek = await svc.deputies.peek(deputy, uid)
        if peek.status == "invalid":
            await message.answer("❌ Приглашение больше недействительно.", reply_markup=ckb.back_to_menu())
            return
        if peek.status == "claimed_by_other":
            await message.answer(DeputyInviteClaimed().user_message, reply_markup=ckb.back_to_menu())
            return
        await message.answer(
            "🛡 <b>Приглашение заместителя</b>\n\nВас приглашают стать заместителем главного администратора турнира.",
            reply_markup=akb.deputy_invitation(deputy),
        )
        return
    if player is not None:
        peek = await svc.team.peek_invite(uid, player)
        blocked = _player_status_message(peek)
        if blocked:
            await message.answer(blocked[0], reply_markup=blocked[1])
            return
        v = peek.team
        await message.answer(
            "👥 <b>Приглашение в команду</b>\n\n"
            f"🏆 Команда: <b>{esc(v.name)}</b>\n👑 Капитан: {v.captain.display}\n\n"
            f"Участников: {v.size}/{MAX_TEAM_SIZE}",
            reply_markup=tkb.invitation(player),
        )
        return
    peek = await svc.team.peek_captain_invite(uid, captain)
    blocked = _captain_status_message(peek)
    if blocked:
        await message.answer(blocked[0], reply_markup=blocked[1])
        return
    v = peek.team
    await message.answer(
        "👑 <b>Приглашение капитана</b>\n\n"
        f"🏆 Команда: <b>{esc(v.name)}</b>\n👥 Состав: {v.size}/{MAX_TEAM_SIZE}\n\n"
        "Вам предлагают стать капитаном этой команды.",
        reply_markup=tkb.captain_invitation(captain),
    )


@router.callback_query(F.data.regexp(rf"^dep:acc:{_TOKEN}$"))
async def deputy_accept(cb: CallbackQuery, svc: Services) -> None:
    """Any user may claim an open deputy invite - the token, not their identity, grants nothing by
    itself: they become a mere PENDING candidate; only the main admin's separate approval (in
    admin_deputies.py) grants real deputy rights."""
    await cb.answer()
    token = cb.data.split(":", 2)[2]
    peek = await svc.deputies.peek(token, cb.from_user.id)
    if peek.status == "invalid":
        await show(cb, "❌ Приглашение больше недействительно.", ckb.back_to_menu())
        return
    if peek.status == "claimed_by_other":
        await show(cb, DeputyInviteClaimed().user_message, ckb.back_to_menu())
        return
    user = tg_user(cb)
    try:
        inv = await svc.deputies.claim(user, token)
    except (DeputyInviteClaimed, NotFound) as e:
        await show(cb, e.user_message, ckb.back_to_menu())
        return
    await show(
        cb,
        "✅ Заявка отправлена главному администратору.\n\nВы получите доступ, как только он подтвердит заявку.",
        ckb.back_to_menu(),
    )
    await _notify(
        svc, svc.cfg.admin_id,
        f"🛡 <b>Новый заместитель</b>\n\n👤 Имя: {person(user.full_name, user.username, user.id)}\n"
        f"🆔 Telegram ID: {inv['candidate_id']}\n\nОткройте «🛡 Заместители» → «Заявки на рассмотрении», чтобы подтвердить.",
    )


@router.callback_query(F.data.regexp(rf"^join:ok:{_TOKEN}$"))
async def join_confirm(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    token = cb.data.split(":", 2)[2]
    user = tg_user(cb)
    # cheap read-only checks first (no Telegram API call for hopeless cases)
    peek = await svc.team.peek_invite(user.id, token)
    blocked = _player_status_message(peek)
    if blocked:
        await show(cb, blocked[0], blocked[1])
        return
    res = await svc.sub.check(user.id)
    if not res.ok:
        from app.handlers.registration import sub_message
        await show(cb, sub_message(res), ckb.subscribe(svc.cfg, retry_cb=cb.data, cancel_cb="menu"))
        return
    try:
        result = await svc.team.join_team(user, token)  # authoritative: row lock + DB triggers
    except ServiceError as e:
        await show(cb, e.user_message, ckb.back_to_menu())
        return
    v = result.team
    if result.already_member:
        await show(cb, "✅ Вы уже состоите в этой команде.", tkb.my_team_only())
        return
    await show(
        cb,
        f"✅ <b>Вы вступили в команду!</b>\n\n🏆 <b>{esc(v.name)}</b>\n👥 Участников: {v.size}/{MAX_TEAM_SIZE}",
        tkb.my_team_only(),
    )
    me = next(m for m in v.members if m.user_id == user.id)
    await _notify(
        svc, v.captain_id,
        f"👤 <b>Новый участник команды</b>\n\n{me.display} присоединился к команде.\n\n👥 Состав: {v.size}/{MAX_TEAM_SIZE}",
    )


@router.callback_query(F.data.regexp(rf"^cap:ok:{_TOKEN}$"))
async def captain_confirm(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    token = cb.data.split(":", 2)[2]
    user = tg_user(cb)
    peek = await svc.team.peek_captain_invite(user.id, token)
    blocked = _captain_status_message(peek)
    if blocked:
        await show(cb, blocked[0], blocked[1])
        return
    res = await svc.sub.check(user.id)
    if not res.ok:
        from app.handlers.registration import sub_message
        await show(cb, sub_message(res), ckb.subscribe(svc.cfg, retry_cb=cb.data, cancel_cb="menu"))
        return
    try:
        result = await svc.team.accept_captain(user, token)  # atomic: token + member + captain + limit
    except InvalidInvite:
        await show(cb, "❌ Приглашение больше недействительно.", ckb.back_to_menu())
        return
    except ServiceError as e:
        await show(cb, e.user_message, ckb.back_to_menu())
        return
    v = result.team
    if result.already_captain:
        await show(cb, ALREADY_CAPTAIN_MSG, tkb.my_team_only())
        return
    await show(
        cb,
        f"✅ <b>Вы стали капитаном команды!</b>\n\n🏆 <b>{esc(v.name)}</b>\n👥 Участников: {v.size}/{MAX_TEAM_SIZE}\n\n"
        f"Теперь можно приглашать игроков (свободных мест: {v.free}).",
        tkb.after_create(),
    )
    if v.created_by != user.id:
        me = v.captain
        await _notify(svc, v.created_by, f"👑 <b>Капитан назначен</b>\n\n{me.display} стал капитаном команды «{esc(v.name)}».\n👥 Состав: {v.size}/{MAX_TEAM_SIZE}")


# ------------------------------------------------------------------ team card
async def _my_team(cb: CallbackQuery, svc: Services) -> Optional[TeamView]:
    view = await svc.team.view_for_user(cb.from_user.id)
    if view is None:
        text, markup = await menu_view(svc, cb.from_user.id)
        await show(cb, "Вы не состоите в команде.\n\n" + text, markup)
    return view


async def _captain_team(cb: CallbackQuery, svc: Services) -> Optional[TeamView]:
    view = await _my_team(cb, svc)
    if view is not None and view.captain_id != cb.from_user.id:
        await show(cb, NotCaptain().user_message, tkb.back_to_team())
        return None
    return view


async def _registrar_pending_team(cb: CallbackQuery, svc: Services) -> Optional[TeamView]:
    """The user's own registration while no captain is assigned (the only time registrar tools exist)."""
    view = await svc.team.registrar_view(cb.from_user.id)
    if view is None:
        await show(cb, "У вас нет созданной регистрации команды.", ckb.back_to_menu())
        return None
    if not view.captain_pending:
        await show(cb, "ℹ️ Капитан уже назначен — управление командой перешло к нему.", tkb.back_to_team())
        return None
    return view


async def _invite_text(svc: Services, view: TeamView, head: str) -> str:
    link = await svc.invite_link(view.invite_token)
    tail = (
        "Пока в команде меньше 5 человек, ссылка работает."
        if view.free else "Сейчас команда заполнена (5/5). Ссылка снова заработает, когда освободится место."
    )
    return f"{head}\n\n👥 Состав: {view.size}/{MAX_TEAM_SIZE}\nСвободных мест: {view.free}\n\n🔗 Ссылка для вступления:\n<code>{esc(link)}</code>\n\n{tail}"


async def _captain_link_text(svc: Services, view: TeamView, head: str) -> str:
    link = await svc.captain_link(view.captain_invite_token)
    return (
        f"{head}\n\n🏆 {esc(view.name)}\n👥 Состав: {view.size}/{MAX_TEAM_SIZE}\n\n"
        f"👑 Ссылка для капитана:\n<code>{esc(link)}</code>\n\n"
        "Отправьте её будущему капитану. Ссылка одноразовая: после назначения капитана она перестаёт работать. "
        "Если отправили не тому человеку — обновите ссылку, старая сразу станет недействительной."
    )


def card_markup(view: TeamView, uid: int, extra_reg: bool = False):
    role = view.role_of(uid) or "registrar"
    kb_ = tkb.team_card(
        role, pending=view.captain_pending, is_creator=view.created_by == uid,
        can_transfer=role == "captain" and view.size > 1,
    )
    if extra_reg:  # the user also registered ANOTHER team (they only registered it, they play elsewhere)
        kb_.inline_keyboard.insert(-1, [tkb.B(text="📝 Команда, которую я зарегистрировал", callback_data="team:reg")])
    return kb_


@router.callback_query(F.data == "team")
async def team_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _my_team(cb, svc)
    if not view:
        return
    rv = await svc.team.registrar_view(cb.from_user.id)
    await show(cb, format_team(view, cb.from_user.id), card_markup(view, cb.from_user.id, bool(rv and rv.team_id != view.team_id)))


@router.callback_query(F.data == "team:reg")
async def registrar_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    rv = await svc.team.registrar_view(cb.from_user.id)
    if rv is None:
        await show(cb, "У вас нет созданной регистрации команды.", ckb.back_to_menu())
        return
    await show(cb, format_team(rv, cb.from_user.id), tkb.team_card("registrar", pending=rv.captain_pending, is_creator=True))


@router.callback_query(F.data == "team:inv")
async def invite(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _captain_team(cb, svc)
    if view:
        await show(cb, await _invite_text(svc, view, "🔗 <b>Пригласить игрока</b>"), tkb.back_to_team())


# ------------------------------------------------------------------ registrar tools (only while no captain)
@router.callback_query(F.data == "team:cap")
async def captain_link(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _registrar_pending_team(cb, svc)
    if view:
        await show(cb, await _captain_link_text(svc, view, "👑 <b>Ссылка для капитана</b>"), tkb.captain_link_screen())


@router.callback_query(F.data == "team:capreset")
async def captain_link_reset(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        view = await svc.team.regenerate_captain_token(cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, tkb.back_to_team())
        return
    await show(cb, await _captain_link_text(svc, view, "✅ <b>Ссылка капитана обновлена</b>\nСтарая ссылка больше не работает."), tkb.captain_link_screen())


@router.callback_query(F.data == "team:cancel")
async def cancel_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _registrar_pending_team(cb, svc)
    if view:
        await show(
            cb, f"⚠️ <b>Отменить регистрацию?</b>\n\nКоманда «{esc(view.name)}» будет удалена вместе с составом ({view.size}/{MAX_TEAM_SIZE}).",
            tkb.confirm("team:cancelok", "team"),
        )


@router.callback_query(F.data == "team:cancelok")
async def cancel_ok(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        name = await svc.team.cancel_registration(cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, ckb.back_to_menu())
        return
    await show(cb, f"✅ Регистрация команды «{esc(name)}» отменена.", ckb.back_to_menu())


# ------------------------------------------------------------------ roster management (captain)
@router.callback_query(F.data == "team:mng")
async def manage(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _captain_team(cb, svc)
    if not view:
        return
    if view.size <= 1:
        await show(cb, "🔄 <b>Управление составом</b>\n\nКроме вас, в команде пока никого нет.", tkb.back_to_team())
        return
    await show(cb, f"🔄 <b>Управление составом</b>\n\n👥 Состав: {view.size}/{MAX_TEAM_SIZE}\nВыберите участника:", tkb.manage_list(view.members))


async def _target(cb: CallbackQuery, svc: Services):
    """(view, member) for a captain acting on a non-captain member of *their own* team, else (None, None)."""
    view = await _captain_team(cb, svc)
    if not view:
        return None, None
    uid = parse_int(cb.data.rsplit(":", 1)[1])
    member = next((m for m in view.members if m.user_id == uid and not m.is_captain), None)
    if member is None:
        await show(cb, "Игрок не найден в вашей команде (возможно, уже удалён).", tkb.back_to_team())
        return None, None
    return view, member


@router.callback_query(F.data.regexp(r"^team:m:\d{1,18}$"))
async def member_card(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view, m = await _target(cb, svc)
    if m:
        await show(cb, f"👤 <b>Участник</b>\n\n{m.display}", tkb.member_card(m.user_id))


@router.callback_query(F.data.regexp(r"^team:rp:\d{1,18}$"))
async def replace_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view, m = await _target(cb, svc)
    if m:
        await show(
            cb,
            f"⚠️ <b>Заменить участника?</b>\n\n{m.display} будет удалён из команды.\n"
            "После этого освободится одно место для нового участника.",
            tkb.confirm(f"team:rpok:{m.user_id}", f"team:m:{m.user_id}"),
        )


@router.callback_query(F.data.regexp(r"^team:rm:\d{1,18}$"))
async def remove_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view, m = await _target(cb, svc)
    if m:
        await show(
            cb, f"⚠️ <b>Удалить из команды?</b>\n\n{m.display} будет удалён из команды.",
            tkb.confirm(f"team:rmok:{m.user_id}", f"team:m:{m.user_id}"),
        )


async def _do_remove(cb: CallbackQuery, svc: Services, *, replace: bool) -> None:
    await cb.answer()
    uid = parse_int(cb.data.rsplit(":", 1)[1])
    try:
        removed, view = await svc.team.remove_member(cb.from_user.id, uid)  # NotCaptain / captain-protection inside
    except ServiceError as e:
        await show(cb, e.user_message, tkb.back_to_team())
        return
    head = (
        f"✅ <b>{removed.display}</b> удалён из команды. Освободилось одно место — отправьте ссылку новому участнику."
        if replace else f"✅ <b>{removed.display}</b> удалён из команды."
    )
    await show(cb, await _invite_text(svc, view, head), tkb.back_to_team())
    await _notify(svc, removed.user_id, f"ℹ️ Капитан удалил вас из команды «{esc(view.name)}».")


@router.callback_query(F.data.regexp(r"^team:rpok:\d{1,18}$"))
async def replace_ok(cb: CallbackQuery, svc: Services) -> None:
    await _do_remove(cb, svc, replace=True)


@router.callback_query(F.data.regexp(r"^team:rmok:\d{1,18}$"))
async def remove_ok(cb: CallbackQuery, svc: Services) -> None:
    await _do_remove(cb, svc, replace=False)


# ------------------------------------------------------------------ transfer of captaincy
@router.callback_query(F.data == "team:tr")
async def transfer_list(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _captain_team(cb, svc)
    if not view:
        return
    if view.size <= 1:
        await show(cb, "👑 <b>Передать капитанство</b>\n\nВ команде нет других участников.", tkb.back_to_team())
        return
    await show(cb, "👑 <b>Передать капитанство</b>\n\nВыберите нового капитана из участников команды:", tkb.transfer_list(view.members))


@router.callback_query(F.data.regexp(r"^team:trc:\d{1,18}$"))
async def transfer_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view, m = await _target(cb, svc)  # only an existing, non-captain member of the caller's own team
    if m:
        await show(
            cb, f"⚠️ <b>Передать капитанство?</b>\n\nНовый капитан:\n{m.display}\n\nВы станете обычным участником.",
            tkb.confirm(f"team:trok:{m.user_id}", "team"),
        )


@router.callback_query(F.data.regexp(r"^team:trok:\d{1,18}$"))
async def transfer_ok(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    uid = parse_int(cb.data.rsplit(":", 1)[1])
    try:
        new, old, view = await svc.team.transfer_captaincy(cb.from_user.id, uid)
    except ServiceError as e:
        await show(cb, e.user_message, tkb.my_team_only())
        return
    await show(
        cb, f"✅ <b>Капитанство передано</b>\n\nНовый капитан: {new.display}\n👥 Состав: {view.size}/{MAX_TEAM_SIZE}",
        tkb.my_team_only(),
    )
    await _notify(svc, new.user_id, f"👑 Вам передано капитанство команды «{esc(view.name)}».")


# ------------------------------------------------------------------ leave
@router.callback_query(F.data == "team:leave")
async def leave_ask(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await _my_team(cb, svc)
    if not view:
        return
    role = view.role_of(cb.from_user.id)
    if role == "captain":
        await show(cb, "⛔ Капитан не может покинуть команду. Передайте капитанство другому участнику или обратитесь к администратору.", tkb.back_to_team())
        return
    if role != "member":
        await show(cb, "Вы не входите в игровой состав этой команды.", tkb.back_to_team())
        return
    await show(
        cb, f"⚠️ <b>Покинуть команду?</b>\n\n🏆 {esc(view.name)}\nВаше место освободится.",
        tkb.confirm("team:leaveok", "team"),
    )


@router.callback_query(F.data == "team:leaveok")
async def leave_ok(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    try:
        me, view = await svc.team.leave_team(cb.from_user.id)
    except ServiceError as e:
        await show(cb, e.user_message, ckb.back_to_menu())
        return
    await show(cb, f"✅ Вы покинули команду «{esc(view.name)}».", ckb.back_to_menu())
    if view.captain_id:
        await _notify(svc, view.captain_id, f"ℹ️ {me.display} покинул(а) команду.\n\n👥 Состав: {view.size}/{MAX_TEAM_SIZE}")


@router.callback_query(F.data == "sub:invite")
async def sub_invite(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer()
    view = await svc.team.view_for_user(cb.from_user.id)
    if view is None or view.captain_id != cb.from_user.id:
        await show(cb, "⛔ Только капитан может приглашать запасных.", tkb.back_to_team())
        return
    link = await svc.invite_link(view.invite_token)
    await show(
        cb,
        f"🔗 Ссылка для запасного:\n<code>{link}</code>\n\n"
        "Отправьте её игроку. После вступления отметьте его как запасного в составе.",
        tkb.back_to_team(),
    )
