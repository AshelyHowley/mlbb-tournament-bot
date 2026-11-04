"""Domain errors. `user_message` is safe HTML for the end user (values already escaped)."""
from __future__ import annotations

from app.utils.text import esc


class ServiceError(Exception):
    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class ValidationError(ServiceError):
    pass


class RegistrationClosed(ServiceError):
    def __init__(self):
        super().__init__("🔒 Регистрация сейчас закрыта.")


class AlreadyRegistered(ServiceError):
    pass


class TeamNameTaken(ServiceError):
    def __init__(self):
        super().__init__("❌ Команда с таким названием уже зарегистрирована. Выберите другое название.")


class NotFound(ServiceError):
    def __init__(self, what: str = "Запись"):
        super().__init__(f"{esc(what)} не найдена (возможно, уже удалена).")


class Conflict(ServiceError):
    pass


class InvalidInvite(ServiceError):
    def __init__(self):
        super().__init__("❌ Приглашение недействительно или команда больше не существует.")


class TeamFull(ServiceError):
    def __init__(self):
        super().__init__("❌ В команде больше нет свободных мест.")


class AlreadyInOtherTeam(ServiceError):
    def __init__(self):
        super().__init__("⚠️ Вы уже состоите в другой команде.\n\nСначала необходимо покинуть предыдущую команду.")


class NotCaptain(ServiceError):
    def __init__(self):
        super().__init__("⛔ Это действие доступно только капитану команды.")


class CaptainRequired(ServiceError):
    def __init__(self):
        super().__init__("⏳ Капитан этой команды пока не назначен. Набор игроков откроется после назначения капитана.")


class CaptainExists(ServiceError):
    def __init__(self):
        super().__init__("⚠️ У этой команды уже назначен капитан.")


class TeamNotEligible(ServiceError):
    def __init__(self, name: str = "", size: int = 0):
        extra = f"\n\n👥 {esc(name)}: состав {size}/5" if name else ""
        super().__init__("⚠️ <b>Команда не готова</b>\n\nДля участия в матче необходимо минимум 2 игрока." + extra)


class SeriesDecided(ServiceError):
    def __init__(self):
        super().__init__("❌ Серия уже завершена (сыграно 5 каток либо у одной из команд уже 3 победы).")


class InvalidGameWinner(ServiceError):
    def __init__(self):
        super().__init__("❌ Победителем катки может быть только одна из двух команд этой серии.")


class ResultLocked(ServiceError):
    def __init__(self):
        super().__init__("⛔ Нельзя изменить результат: одна из команд уже участвует в следующем матче.")


class DeputyLimitReached(ServiceError):
    def __init__(self):
        super().__init__("⚠️ У главного администратора уже 2 заместителя. Сначала снимите одного из них.")


class DeputyInviteClaimed(ServiceError):
    def __init__(self):
        super().__init__("⚠️ Это приглашение уже используется другим пользователем.")


class NotMainAdmin(ServiceError):
    def __init__(self):
        super().__init__("⛔ Это действие доступно только главному администратору.")


class NotStaff(ServiceError):
    def __init__(self):
        super().__init__("⛔ Недостаточно прав.")


class TournamentNotReady(ServiceError):
    pass
