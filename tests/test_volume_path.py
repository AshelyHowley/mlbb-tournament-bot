"""On Railway the SQLite file must live on the Volume, or every deploy wipes registrations."""
from app.config import load_config, place_on_volume

VOL = {"RAILWAY_VOLUME_MOUNT_PATH": "/data"}


def test_relative_sqlite_path_moves_onto_volume():
    assert place_on_volume("sqlite+aiosqlite:///./tournament.db", VOL) == "sqlite+aiosqlite:////data/tournament.db"
    assert place_on_volume("sqlite:///tournament.db", {"RAILWAY_VOLUME_MOUNT_PATH": "/data/"}) == "sqlite:////data/tournament.db"


def test_no_volume_or_explicit_path_is_left_alone():
    assert place_on_volume("sqlite+aiosqlite:///./tournament.db", {}) == "sqlite+aiosqlite:///./tournament.db"
    assert place_on_volume("sqlite+aiosqlite:////srv/x.db", VOL) == "sqlite+aiosqlite:////srv/x.db"
    assert place_on_volume("sqlite+aiosqlite:///C:/bot/x.db", VOL) == "sqlite+aiosqlite:///C:/bot/x.db"


def test_load_config_uses_volume(tmp_path):
    env = {
        "BOT_TOKEN": "123456:" + "A" * 35,
        "ADMIN_TELEGRAM_ID": "42",
        "DATABASE_URL": "sqlite+aiosqlite:///./tournament.db",
        **VOL,
    }
    cfg = load_config(env_file=tmp_path / "missing.env", environ=env)
    assert cfg.database_url == "sqlite+aiosqlite:////data/tournament.db"
