import pytest

from app.config import ConfigError, load_config, load_database_url

TOKEN = "123456789:" + "A" * 35
DSN = "postgresql://mlbb_bot:s3cret@127.0.0.1:5432/mlbb_tournament"
GOOD = {"BOT_TOKEN": TOKEN, "ADMIN_TELEGRAM_ID": "42", "DATABASE_URL": DSN}


def load(env, tmp_path):
    return load_config(env_file=tmp_path / "none.env", environ=env)


def test_valid_config_and_defaults(tmp_path):
    cfg = load(GOOD, tmp_path)
    assert cfg.admin_id == 42
    assert cfg.channel_id == "@yuliataetae" and cfg.chat_id == "@MLBangBangTournamentChat"
    assert cfg.chat_url == "https://t.me/MLBangBangTournamentChat"


def test_missing_required_names_variables_without_values(tmp_path):
    with pytest.raises(ConfigError) as e:
        load({}, tmp_path)
    msg = str(e.value)
    assert "BOT_TOKEN" in msg and "ADMIN_TELEGRAM_ID" in msg and "DATABASE_URL" in msg


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "1.5", ""])
def test_admin_id_must_be_positive_int(bad, tmp_path):
    env = {**GOOD, "ADMIN_TELEGRAM_ID": bad}
    with pytest.raises(ConfigError):
        load(env, tmp_path)


def test_placeholder_password_rejected(tmp_path):
    env = {**GOOD, "DATABASE_URL": "postgresql://mlbb_bot:PASSWORD@127.0.0.1:5432/mlbb_tournament"}
    with pytest.raises(ConfigError, match="password"):
        load(env, tmp_path)


@pytest.mark.parametrize("url", ["mysql://a:b@h/db", "postgresql://a:b@h", "postgresql://:b@h/db", "nonsense"])
def test_bad_database_url(url, tmp_path):
    with pytest.raises(ConfigError):
        load({**GOOD, "DATABASE_URL": url}, tmp_path)


def test_bad_token_format(tmp_path):
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load({**GOOD, "BOT_TOKEN": "nope"}, tmp_path)


def test_secrets_never_in_repr_or_error(tmp_path):
    cfg = load(GOOD, tmp_path)
    assert TOKEN not in repr(cfg) and "s3cret" not in repr(cfg)
    assert TOKEN in cfg.secrets() and "s3cret" in cfg.secrets()
    with pytest.raises(ConfigError) as e:
        load({**GOOD, "BOT_TOKEN": "s3cret-bad"}, tmp_path)
    assert "s3cret" not in str(e.value)


def test_env_file_is_read_and_environ_overrides(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"BOT_TOKEN={TOKEN}\nADMIN_TELEGRAM_ID=1\nDATABASE_URL={DSN}\n", encoding="utf-8")
    assert load_config(env_file=f, environ={}).admin_id == 1
    assert load_config(env_file=f, environ={"ADMIN_TELEGRAM_ID": "7"}).admin_id == 7


def test_load_database_url_only(tmp_path):
    assert load_database_url(tmp_path / "x.env", {"DATABASE_URL": DSN}) == DSN
    with pytest.raises(ConfigError):
        load_database_url(tmp_path / "x.env", {})


def test_gitignore_protects_env():
    from pathlib import Path

    text = (Path(__file__).parent.parent / ".gitignore").read_text()
    assert ".env" in text.splitlines()
