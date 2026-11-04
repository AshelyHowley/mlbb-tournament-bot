"""Configuration loader. Never prints or repr()s secret values."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_CHAT_RE = re.compile(r"^(@[A-Za-z][A-Za-z0-9_]{4,31}|-?\d{5,20})$")

_DEFAULTS = {
    "CHANNEL_ID": "@yuliataetae",
    "CHANNEL_URL": "https://t.me/yuliataetae",
    "CHAT_ID": "@MLBangBangTournamentChat",
    "CHAT_URL": "https://t.me/MLBangBangTournamentChat",
    "TIMEZONE": "Europe/Moscow",
    "ACCESS_MODE": "private",  # fail closed: an absent setting never opens the bot to everyone
}
_KEYS = ("BOT_TOKEN", "ADMIN_TELEGRAM_ID", "DATABASE_URL", "ALLOWED_USER_IDS", *_DEFAULTS)
ACCESS_MODES = ("private", "public")
_MAX_TG_ID = 2**63 - 1


def parse_allowed_ids(raw: str) -> frozenset[int]:
    """'1, 2,3,' -> {1,2,3}. Raises ValueError (without echoing the input) on anything malformed."""
    ids: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue  # tolerate a trailing comma / empty setting
        if not (token.isascii() and token.isdigit()) or not (0 < int(token) <= _MAX_TG_ID):
            raise ValueError("invalid Telegram ID")
        ids.add(int(token))
    return frozenset(ids)


class ConfigError(Exception):
    """Raised with a human-readable (secret-free) message."""


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    admin_id: int
    database_url: str = field(repr=False)
    channel_id: str
    channel_url: str
    chat_id: str
    chat_url: str
    timezone: str = "Europe/Moscow"
    access_mode: str = "private"
    allowed_user_ids: frozenset = frozenset()

    # Access and admin rights are deliberately separate questions.
    def is_bot_admin(self, user_id: int) -> bool:
        """Only ADMIN_TELEGRAM_ID. Telegram chat/channel admin status is never consulted."""
        return user_id == self.admin_id

    def can_access_bot(self, user_id: int) -> bool:
        """Fail closed: anything other than the exact mode 'public' requires the allowlist."""
        if self.access_mode == "public":
            return True
        return user_id == self.admin_id or user_id in self.allowed_user_ids

    def secrets(self) -> list[str]:
        """Values that must never reach logs."""
        out = [self.bot_token, self.database_url]
        pwd = urlsplit(self.database_url).password
        if pwd:
            out.append(pwd)
        return [s for s in out if s]


def _read(env_file: Optional[Path], environ: Optional[Mapping[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    path = env_file if env_file is not None else PROJECT_ROOT / ".env"
    if path.exists():
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    source = os.environ if environ is None else environ
    values.update({k: source[k] for k in _KEYS if k in source and source[k] != ""})
    return {k: v.strip() for k, v in values.items() if k in _KEYS}


def validate_database_url(url: str) -> Optional[str]:
    """Return an error string (without secrets) or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "DATABASE_URL is not a valid URL"
    if parts.scheme in ("sqlite", "sqlite+aiosqlite"):
        return None
    host, dbname = parts.hostname, parts.path.lstrip("/")
    pwd = parts.password
    if parts.scheme not in ("postgresql", "postgres"):
        return "DATABASE_URL must start with postgresql:// or sqlite+aiosqlite://"
    if not host or not dbname or not parts.username:
        return "DATABASE_URL must contain user, host and database name"
    if not pwd or pwd == "PASSWORD":
        return "DATABASE_URL still contains the placeholder/empty password - fill it in .env"
    return None


def load_database_url(env_file: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> str:
    v = _read(env_file, environ)
    url = v.get("DATABASE_URL", "")
    if not url:
        raise ConfigError("Missing required setting: DATABASE_URL")
    err = validate_database_url(url)
    if err:
        raise ConfigError(err)
    return url


def load_config(env_file: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> Config:
    v = _read(env_file, environ)
    for k, d in _DEFAULTS.items():
        v.setdefault(k, d)

    problems: list[str] = []
    missing = [k for k in ("BOT_TOKEN", "ADMIN_TELEGRAM_ID", "DATABASE_URL") if not v.get(k)]
    if missing:
        problems.append("Missing required settings: " + ", ".join(missing))

    if v.get("BOT_TOKEN") and not _TOKEN_RE.match(v["BOT_TOKEN"]):
        problems.append("BOT_TOKEN has an invalid format")

    admin_id = 0
    if v.get("ADMIN_TELEGRAM_ID"):
        if v["ADMIN_TELEGRAM_ID"].isdigit() and int(v["ADMIN_TELEGRAM_ID"]) > 0:
            admin_id = int(v["ADMIN_TELEGRAM_ID"])
        else:
            problems.append("ADMIN_TELEGRAM_ID must be a positive integer")

    if v.get("DATABASE_URL"):
        err = validate_database_url(v["DATABASE_URL"])
        if err:
            problems.append(err)

    for key in ("CHANNEL_ID", "CHAT_ID"):
        if not _CHAT_RE.match(v[key]):
            problems.append(f"{key} must look like @username or a numeric id")
    for key in ("CHANNEL_URL", "CHAT_URL"):
        if not v[key].startswith("https://t.me/"):
            problems.append(f"{key} must start with https://t.me/")

    access_mode = v["ACCESS_MODE"].strip().lower()
    if access_mode not in ACCESS_MODES:
        problems.append("ACCESS_MODE must be 'private' or 'public'")
    allowed: frozenset[int] = frozenset()
    try:
        allowed = parse_allowed_ids(v.get("ALLOWED_USER_IDS", ""))
    except ValueError:
        problems.append("ALLOWED_USER_IDS must be a comma-separated list of positive integer Telegram IDs")

    if problems:
        raise ConfigError("; ".join(problems))

    return Config(
        access_mode=access_mode,
        allowed_user_ids=allowed,
        bot_token=v["BOT_TOKEN"],
        admin_id=admin_id,
        database_url=v["DATABASE_URL"],
        channel_id=v["CHANNEL_ID"],
        channel_url=v["CHANNEL_URL"],
        chat_id=v["CHAT_ID"],
        chat_url=v["CHAT_URL"],
        timezone=v["TIMEZONE"],
    )
