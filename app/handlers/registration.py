from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.context import Services
from app.handlers.common import format_solo, menu_view, require_username, show, tg_user
from app.keyboards import common as kbs
from app.keyboards import team as tkb
from app.services.errors import ServiceError, TeamNameTaken
from app.services.registration import ROLES
from app.services.subscription import SubscriptionResult
from app.services.team import CREATOR_ROLES, MAX_TEAM_SIZE
from app.utils.text import clean_team_name, esc, person

log = logging.getLogger(__name__)
router = Router(name="registration")


class RegTeam(StatesGroup):
    name = State()
    role = State()
    stars = State()
    position = State()
    confirm = State()


class RegSolo(StatesGroup):
    role = State()
    stars = State()
    confirm = State()


_ANY_REG = StateFilter(RegTeam, RegSolo)

ROLE_TITLES = {
    "captain": "👑 Капитан (вы входите в состав)",
    "member": "🎮 Участник, капитан другой (вы входите в состав)",
    "registrar": "📝 Только регистрирую (в состав не вхожу)",
}
ROLE_PROMPT = "👤 <b>Ваша роль в этой команде?</b>"


async def _to_menu(event, state: FSMContext, svc: Services, prefix: str = "") -> None:
    await state.clear()
    text, markup = await menu_view(svc, event.from_user.id)
    await show(event, prefix + text, markup)


# ---------------------------------------------------------------- entry
@router.callback_query(F.data == "reg:start")
async def reg_start(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await state.clear()
    reg = await svc.reg.my_registration(cb.from_user.id)
    if reg is not None and reg.plays:  # a registrar-only user does not play, so they may still register
        await _to_menu(cb, state, svc, "ℹ️ Вы уже зарегистрированы.\n\n")
        return
    if not await svc.reg.is_open():
        await show(cb, "🔒 Регистрация сейчас закрыта.", kbs.back_to_menu())
        return
    if not await require_username(cb, "reg:start"):
        return
    await show(cb, "Выберите тип регистрации:", kbs.reg_type())


async def _guard(cb: CallbackQuery, state: FSMContext, svc: Services) -> bool:
    """Common preconditions for entering a registration flow."""
    await state.clear()
    reg = await svc.reg.my_registration(cb.from_user.id)
    if reg is not None and reg.plays:
        await _to_menu(cb, state, svc, "ℹ️ Вы уже зарегистрированы.\n\n")
        return False
    if not await svc.reg.is_open():
        await show(cb, "🔒 Регистрация сейчас закрыта.", kbs.back_to_menu())
        return False
    return await require_username(cb, cb.data)


# ---------------------------------------------------------------- team flow: name -> creator role -> confirm -> create
NAME_PROMPT = (
    "👥 <b>Регистрация команды</b>\n\nВ команде максимум 5 игроков, включая капитана. "
    "Игроков вы пригласите по личной ссылке. У каждого игрока должен быть username в Telegram.\n\nВведите <b>название команды</b> (2–40 символов):"
)


@router.callback_query(F.data == "reg:team")
async def team_begin(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    if not await _guard(cb, state, svc):
        return
    reg = await svc.reg.my_registration(cb.from_user.id)
    if reg is not None and reg.has_team and not svc.cfg.is_bot_admin(cb.from_user.id):
        await show(cb, "ℹ️ Вы уже зарегистрировали команду. Откройте «Моя команда».", tkb.my_team_only())
        return
    await _load_archive(state, svc, cb.from_user.id)
    await state.set_state(RegTeam.name)
    await show(cb, NAME_PROMPT, kbs.nav(back=False))


# ---------------------------------------------------------------- player archive helpers
TEAM_MAX_STARS = 50000
SOLO_MAX_STARS = 10000
TEAM_STARS_PROMPT = "⭐ Укажите ваш максимальный ранг (число):"
SOLO_STARS_PROMPT = "⭐ Сколько звёзд в Максе?\nНапишите число (например 1000):"
POSITION_PROMPT = "📝 Кем вы являетесь? (должность)\nНапример: Менеджер, Организатор, Тренер..."


async def _load_archive(state: FSMContext, svc: Services, user_id: int) -> None:
    """Put the player's archived data (if any) into the FSM so prompts can offer to reuse it."""
    a = await svc.archive.get(user_id)
    if a is None:
        return
    await state.update_data(arch_stars=a.stars, arch_roles=[r for r in a.roles if r in ROLES], arch_pos=a.position)


def _stars_markup(data: dict, limit: int):
    s = data.get("arch_stars")
    if s is not None and 0 <= s <= limit:
        return kbs.keep_value(f"✅ Оставить прежнее: {s} ⭐", "reg:keep:stars")
    return kbs.nav()


def _position_markup(data: dict):
    p = data.get("arch_pos")
    if p:
        return kbs.keep_value(f"✅ Оставить: {p}", "reg:keep:pos")
    return kbs.nav()


@router.message(RegTeam.name, F.text)
async def team_name(message: Message, state: FSMContext, svc: Services) -> None:
    named = clean_team_name(message.text)
    if named is None:
        await message.answer("❌ Название: от 2 до 40 символов, без служебных символов. Попробуйте ещё раз:", reply_markup=kbs.nav(back=False))
        return
    if await svc.team.name_taken(named[1]):
        await message.answer(TeamNameTaken().user_message + "\nВведите другое название:", reply_markup=kbs.nav(back=False))
        return
    await state.update_data(name=named[0])
    await state.set_state(RegTeam.role)
    await message.answer(f"🏆 Команда: <b>{esc(named[0])}</b>\n\n{ROLE_PROMPT}", reply_markup=kbs.team_role())


@router.callback_query(RegTeam.role, F.data.regexp(r"^reg:trole:(captain|member|registrar)$"))
async def team_role(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    role = cb.data.rsplit(":", 1)[1]
    if role not in CREATOR_ROLES:
        return
    await state.update_data(role=role)
    data = await state.get_data()
    if role == "registrar":
        await state.set_state(RegTeam.position)
        await show(cb, POSITION_PROMPT, _position_markup(data))
    else:
        # Captain or member: ask for their rank (one tap to reuse the archived one)
        await state.set_state(RegTeam.stars)
        await show(cb, TEAM_STARS_PROMPT, _stars_markup(data, TEAM_MAX_STARS))


@router.message(RegTeam.name, ~F.text)
@router.message(RegTeam.role)
@router.message(RegSolo.role)
@router.message(RegTeam.confirm)
@router.message(RegSolo.confirm)
async def reg_wrong_input(message: Message) -> None:
    await message.answer("Пожалуйста, используйте кнопки или отправьте текст, как указано выше.")



@router.message(RegTeam.stars)
async def team_stars(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 <= int(text) <= TEAM_MAX_STARS):
        await message.answer(f"Введите число от 0 до {TEAM_MAX_STARS}")
        return
    await _team_stars_done(message, state, int(text))


async def _team_stars_done(event, state: FSMContext, stars: int) -> None:
    await state.update_data(stars=stars)
    data = await state.get_data()
    await state.set_state(RegTeam.confirm)
    role = data.get("role", "—")
    await show(
        event,
        f"📋 <b>Проверьте данные</b>\n\n"
        f"Команда: <b>{esc(data.get('name', '—'))}</b>\n"
        f"Ваша роль: {ROLE_TITLES.get(role, role)}\n"
        f"⭐ Ваш ранг: <b>{stars}</b>",
        kbs.confirm_reg(),
    )



@router.message(RegTeam.position)
async def team_position(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text or len(text) < 2 or len(text) > 40:
        await message.answer("Введите должность от 2 до 40 символов")
        return
    await _team_position_done(message, state, text)


async def _team_position_done(event, state: FSMContext, position: str) -> None:
    await state.update_data(position=position)
    data = await state.get_data()
    await state.set_state(RegTeam.confirm)
    u = event.from_user
    await show(
        event,
        f"📋 <b>Проверьте данные</b>\n\n"
        f"Команда: <b>{esc(data.get('name', '—'))}</b>\n"
        f"Вы: {person(u.full_name, u.username, u.id)}\n"
        f"Ваша роль: {ROLE_TITLES.get('registrar')}\n"
        f"Должность: <b>{esc(position)}</b>\n\n"
        "Вы получите отдельную ссылку для капитана; набор игроков откроется после его назначения.",
        kbs.confirm_reg(),
    )

# ---------------------------------------------------------------- solo flow
@router.callback_query(F.data == "reg:solo")
async def solo_begin(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    if not await _guard(cb, state, svc):
        return
    await _load_archive(state, svc, cb.from_user.id)
    data = await state.get_data()
    arch_roles, arch_stars = data.get("arch_roles"), data.get("arch_stars")
    if arch_roles and arch_stars is not None and 0 <= arch_stars <= SOLO_MAX_STARS:
        # Known player: everything is pre-filled from the archive, they only correct roles / stars.
        await state.update_data(roles=list(arch_roles), stars=arch_stars)
        await _solo_confirm(cb, state, "♻️ <b>Мы нашли ваши данные с прошлой регистрации.</b>\n"
                                       "Проверьте их — при необходимости измените роли или звёзды.\n\n")
        return
    await state.set_state(RegSolo.role)
    await state.update_data(roles=list(arch_roles or []))
    await show(cb, SOLO_ROLES_PROMPT, kbs.roles_multi(arch_roles or []))


SOLO_ROLES_PROMPT = "👤 <b>Соло-регистрация</b>\n\nВыберите роли (можно несколько):"


async def _solo_confirm(event, state: FSMContext, prefix: str = "") -> None:
    data = await state.get_data()
    await state.set_state(RegSolo.confirm)
    u = event.from_user
    await show(
        event,
        prefix + f"📋 <b>Проверьте данные</b>\n\n"
        f"Telegram: {person(u.full_name, u.username, u.id)}\n"
        f"Роли: <b>{', '.join(data.get('roles', []))}</b>\n"
        f"Звёзды: <b>{data.get('stars', 0)}</b>",
        kbs.confirm_reg(edit=True),
    )


@router.callback_query(RegSolo.role, F.data.startswith("reg:role:"))
async def solo_role(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    roles = data.get("roles", [])
    choice = cb.data.split(":", 2)[2]
    if choice == "ALL":
        roles = list(ROLES)
    elif choice == "done":
        if not roles:
            await cb.answer("Выберите хотя бы одну роль", show_alert=True)
            return
        await state.update_data(roles=roles)
        if data.get("stars") is not None:  # editing roles on the confirm screen: stars already known
            await _solo_confirm(cb, state)
            return
        await state.set_state(RegSolo.stars)
        await show(cb, SOLO_STARS_PROMPT, _stars_markup(data, SOLO_MAX_STARS))
        return
    elif choice in ROLES:
        if choice in roles:
            roles.remove(choice)
        else:
            roles.append(choice)
    await state.update_data(roles=roles)
    await show(cb, SOLO_ROLES_PROMPT, kbs.roles_multi(roles))


@router.message(RegSolo.stars)
async def solo_stars(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 <= int(text) <= SOLO_MAX_STARS):
        await message.answer(f"Введите число от 0 до {SOLO_MAX_STARS}")
        return
    await state.update_data(stars=int(text))
    await _solo_confirm(message, state)


# ---------------------------------------------------------------- reuse / correct archived values
@router.callback_query(StateFilter(RegTeam.stars, RegSolo.stars), F.data == "reg:keep:stars")
async def keep_stars(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    stars = data.get("arch_stars")
    if stars is None:
        return
    if await state.get_state() == RegTeam.stars.state:
        await _team_stars_done(cb, state, stars)
    else:
        await state.update_data(stars=stars)
        await _solo_confirm(cb, state)


@router.callback_query(RegTeam.position, F.data == "reg:keep:pos")
async def keep_position(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    pos = (await state.get_data()).get("arch_pos")
    if pos:
        await _team_position_done(cb, state, pos)


@router.callback_query(RegSolo.confirm, F.data == "reg:edit:roles")
async def edit_roles(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    roles = (await state.get_data()).get("roles", [])
    await state.set_state(RegSolo.role)
    await show(cb, SOLO_ROLES_PROMPT, kbs.roles_multi(roles))


@router.callback_query(RegSolo.confirm, F.data == "reg:edit:stars")
async def edit_stars(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    await state.set_state(RegSolo.stars)
    await state.update_data(arch_stars=data.get("stars"))  # "keep" button = the current value
    await show(cb, SOLO_STARS_PROMPT, _stars_markup({"arch_stars": data.get("stars")}, SOLO_MAX_STARS))


# ---------------------------------------------------------------- back / cancel
@router.callback_query(_ANY_REG, F.data == "reg:cancel")
async def reg_cancel(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer("Отменено")
    await _to_menu(cb, state, svc)


@router.callback_query(_ANY_REG, F.data == "reg:back")
async def reg_back(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    cur = await state.get_state()
    if cur == RegTeam.confirm.state:
        await state.set_state(RegTeam.role)
        data = await state.get_data()
        await show(cb, f"🏆 Команда: <b>{esc(data['name'])}</b>\n\n{ROLE_PROMPT}", kbs.team_role())
    elif cur == RegTeam.role.state:
        await state.set_state(RegTeam.name)
        await show(cb, NAME_PROMPT, kbs.nav(back=False))
    elif cur in (RegTeam.stars.state, RegTeam.position.state):
        await state.set_state(RegTeam.role)
        data = await state.get_data()
        await show(cb, f"🏆 Команда: <b>{esc(data['name'])}</b>\n\n{ROLE_PROMPT}", kbs.team_role())
    elif cur in (RegSolo.confirm.state, RegSolo.stars.state):
        await state.set_state(RegSolo.role)
        roles = (await state.get_data()).get("roles", [])
        await show(cb, SOLO_ROLES_PROMPT, kbs.roles_multi(roles))
    else:
        await _to_menu(cb, state, svc)


# ---------------------------------------------------------------- confirm + subscription gate
def sub_message(res: SubscriptionResult) -> str:
    if res.error:
        return "⚠️ Не удалось проверить подписку (ошибка Telegram). Попробуйте чуть позже."
    lines = ["🔔 Для завершения регистрации нужно быть участником канала и чата турнира:\n"]
    lines.append(("✅" if res.channel_ok else "❌") + " Канал")
    lines.append(("✅" if res.chat_ok else "❌") + " Чат")
    lines.append("\nПодпишитесь и нажмите «Проверить подписку».")
    return "\n".join(lines)


async def _team_created_screen(cb: CallbackQuery, svc: Services, view, role: str) -> None:
    head = f"✅ <b>Команда создана!</b>\n\n🏆 Команда: <b>{esc(view.name)}</b>\n👥 Участников: {view.size}/{MAX_TEAM_SIZE}\n"
    if role == "captain":
        link = await svc.invite_link(view.invite_token)
        await show(
            cb, head + f"\nПригласите ещё {view.free} игроков.\n\n🔗 Ссылка для вступления:\n<code>{esc(link)}</code>",
            tkb.after_create(),
        )
        return
    link = await svc.captain_link(view.captain_invite_token)
    who = "Вы — участник команды." if role == "member" else "Вы регистрируете команду и в состав не входите."
    await show(
        cb,
        head + f"👑 Капитан: ожидается\n\n{who}\n\n👑 Ссылка для капитана (отправьте будущему капитану):\n<code>{esc(link)}</code>\n\n"
        "Набор остальных игроков откроется после назначения капитана.",
        tkb.after_create_pending(),
    )


async def _finalize(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    cur = await state.get_state()
    data = await state.get_data()
    if not await require_username(cb, "reg:confirm", "reg:cancel"):  # removed it in the middle of the flow
        return
    res = await svc.sub.check(cb.from_user.id)
    if not res.ok:
        await show(cb, sub_message(res), kbs.subscribe(svc.cfg))
        return
    user = tg_user(cb)
    try:
        if cur == RegTeam.confirm.state:
            stars = data.get("stars", 0)
            position = data.get("position")
            view = await svc.team.create_team(user, data["name"], data.get("role", "captain"),
                                              rank=stars, position=position, is_admin=svc.cfg.is_bot_admin(user.id))
        else:
            roles = data.get("roles", [data.get("role", "EXP")])
            role_str = ",".join(roles) if isinstance(roles, list) else roles
            stars = data.get("stars", 0)
            reg = await svc.reg.register_solo(user, role_str, stars)
    except TeamNameTaken as e:
        await state.set_state(RegTeam.name)
        await show(cb, e.user_message + "\nВведите другое название команды:", kbs.nav(back=False))
        return
    except ServiceError as e:
        await state.clear()
        await show(cb, e.user_message, kbs.back_to_menu())
        return
    await state.clear()
    if cur == RegTeam.confirm.state:
        t_role = data.get("role", "captain")
        await svc.archive.save(
            user.id, user.username, user.full_name, kind=t_role, team_name=data["name"],
            stars=None if t_role == "registrar" else data.get("stars", 0), position=data.get("position"),
        )
    else:
        await svc.archive.save(user.id, user.username, user.full_name, kind="solo", roles=role_str, stars=stars)
    if cur == RegTeam.confirm.state:
        await _team_created_screen(cb, svc, view, data.get("role", "captain"))
    else:
        await show(cb, "🎉 <b>Регистрация завершена!</b>\n\n" + format_solo(reg, user.id), kbs.back_to_menu())


@router.callback_query(StateFilter(RegTeam.confirm, RegSolo.confirm), F.data.in_({"reg:confirm", "sub:check"}))
async def reg_confirm(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await _finalize(cb, state, svc)

