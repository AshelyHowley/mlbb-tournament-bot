"""Input normalisation/validation and output escaping helpers."""
from __future__ import annotations

import html
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Optional

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$")
TEAM_NAME_MIN, TEAM_NAME_MAX = 2, 40
MAX_RAW_INPUT = 100  # anything longer is rejected before regexes run


def esc(value: object) -> str:
    """HTML-escape any user-controlled text."""
    return html.escape(str(value), quote=False)


def normalize_username(raw: Optional[str]) -> Optional[str]:
    """'@Some_Name' / 'some_name' / 'https://t.me/Some_Name' -> 'some_name'; None if malformed."""
    if raw is None or len(raw) > MAX_RAW_INPUT:
        return None
    s = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    if s.startswith("@"):
        s = s[1:]
    s = s.lower()
    return s if USERNAME_RE.match(s) else None


def clean_team_name(raw: Optional[str]) -> Optional[tuple[str, str]]:
    """Returns (display_name, normalised_key) or None when invalid."""
    if raw is None or len(raw) > MAX_RAW_INPUT:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co") for c in raw.replace("\n", " ")):
        return None
    name = " ".join(raw.split())
    if not (TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX):
        return None
    return name, name.casefold()


def fmt_dt(dt, tz_name: str = "Europe/Moscow") -> str:
    if dt is None:
        return "—"
    # SQLite returns ISO strings; Postgres returns datetime
    if isinstance(dt, str):
        from datetime import datetime as _dt
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = _dt.strptime(dt[:19], fmt[:19].replace("%f", ""))
                break
            except ValueError:
                continue
        else:
            return str(dt)[:16]
    try:
        from zoneinfo import ZoneInfo
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(ZoneInfo(tz_name))
    except Exception:
        pass
    return dt.strftime("%d.%m.%Y %H:%M")


def parse_int(value: str, *, max_len: int = 18) -> Optional[int]:
    """Strict positive-integer parser for callback fields."""
    if value.isascii() and value.isdigit() and 0 < len(value) <= max_len:
        n = int(value)
        return n if n > 0 else None
    return None


# ---- identity display & invite tokens -------------------------------------------------------
INVITE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,48}$")
INVITE_PREFIX = "team_"
CAPTAIN_PREFIX = "captain_"
DEPUTY_PREFIX = "deputy_"
TELEGRAM_PAYLOAD_MAX = 64


def new_invite_token() -> str:
    """192 bits of CSPRNG entropy, 32 URL-safe chars (fits Telegram's 64-char start payload with 'team_')."""
    return secrets.token_urlsafe(24)


def _payload_token(args: Optional[str], prefix: str) -> Optional[str]:
    if not args or len(args) > TELEGRAM_PAYLOAD_MAX or not args.startswith(prefix):
        return None
    token = args[len(prefix):]
    return token if INVITE_TOKEN_RE.fullmatch(token) else None  # fullmatch: '$' would accept a trailing newline


def parse_invite_payload(args: Optional[str]) -> Optional[str]:
    """'/start team_<token>' payload -> player-invite token, or None when it is not a well-formed invite."""
    return _payload_token(args, INVITE_PREFIX)


def parse_captain_payload(args: Optional[str]) -> Optional[str]:
    """'/start captain_<token>' payload -> captain-invite token, or None."""
    return _payload_token(args, CAPTAIN_PREFIX)


def parse_deputy_payload(args: Optional[str]) -> Optional[str]:
    """'/start deputy_<token>' payload -> deputy-invite token, or None."""
    return _payload_token(args, DEPUTY_PREFIX)

def person_plain(full_name: Optional[str], username: Optional[str], user_id: Optional[int] = None) -> str:
    """Unescaped display of a Telegram user (for button labels). Username is optional; never None/@None/empty."""
    name = " ".join((full_name or "").split())
    uname = (username or "").strip().lstrip("@")
    if name and uname:
        return f"{name} — @{uname}"
    if name:
        return name
    if uname:
        return f"@{uname}"
    return f"Игрок {user_id}" if user_id else "Игрок"


def person(full_name: Optional[str], username: Optional[str], user_id: Optional[int] = None) -> str:
    """HTML-safe version of person_plain."""
    return esc(person_plain(full_name, username, user_id))