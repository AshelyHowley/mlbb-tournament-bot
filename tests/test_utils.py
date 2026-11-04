import logging

import pytest

from app.utils.logging_setup import RedactingFilter
from app.utils.text import clean_team_name, esc, normalize_username, parse_int


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@Some_Name", "some_name"),
        ("some_name", "some_name"),
        ("  @UPPER123  ", "upper123"),
        ("https://t.me/Abcde", "abcde"),
        ("t.me/Abcde", "abcde"),
    ],
)
def test_username_ok(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "@", "@abc", "@1abcde", "@_abcde", "@ab cde", "@abcde!", "@" + "a" * 33, "x" * 500, "@юзернейм", None,
     "@abc'; DROP TABLE users;--", "<b>abcde</b>"],
)
def test_username_rejected(raw):
    assert normalize_username(raw) is None


def test_username_case_cannot_bypass():
    assert normalize_username("@AbCdE") == normalize_username("@aBcDe")


def test_team_name():
    assert clean_team_name("  Team   Rocket ") == ("Team Rocket", "team rocket")
    assert clean_team_name("ТИМА")[1] == clean_team_name("тима")[1]
    assert clean_team_name("A") is None
    assert clean_team_name("x" * 41) is None
    assert clean_team_name("bad\x00name") is None
    assert clean_team_name("z" * 500) is None


def test_esc_and_parse_int():
    assert esc("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"
    assert parse_int("123") == 123
    for bad in ("", "0", "-1", "1e3", "12a", "٣", "9" * 30):
        assert parse_int(bad) is None


def test_redacting_filter_hides_secrets():
    f = RedactingFilter(["tok:SECRETSECRET", "pa55word"])
    try:
        raise RuntimeError("cannot connect with pa55word")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "token tok:SECRETSECRET used", None, sys.exc_info())
    f.filter(rec)
    assert "SECRETSECRET" not in rec.getMessage()
    assert "pa55word" not in rec.exc_text


def test_person_display_never_shows_none_and_escapes_html():
    from app.utils.text import parse_invite_payload, person

    assert person("Ann", "ann_1234", 5) == "Ann — @ann_1234"
    assert person("Ann", None, 5) == "Ann"
    assert person("", "ann_1234", 5) == "@ann_1234"
    assert person(None, None, 5) == "Игрок 5" and person(None, None) == "Игрок"
    assert person("<b>x</b>", "@u_12345") == "&lt;b&gt;x&lt;/b&gt; — @u_12345"
    for bad in (None, "", "team_", "team_short", "team_" + "a" * 100, "Team_" + "a" * 32, "team_" + "a" * 32 + "\n", "x" + "a" * 32):
        assert parse_invite_payload(bad) is None
    tok = "Ab1_-" * 7  # 35 chars
    assert parse_invite_payload("team_" + tok) == tok


def test_invite_tokens_are_high_entropy_and_unique():
    from app.utils.text import INVITE_TOKEN_RE, new_invite_token

    tokens = {new_invite_token() for _ in range(2000)}
    assert len(tokens) == 2000
    assert all(INVITE_TOKEN_RE.fullmatch(t) and len(t) == 32 for t in tokens)  # 24 random bytes = 192 bits
