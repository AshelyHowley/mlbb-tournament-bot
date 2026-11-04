"""One-off: add 13 test teams to the active tournament. Run from the bot folder."""
import sqlite3
from pathlib import Path

DB = Path("tournament.db")
if not DB.exists():
    raise SystemExit("tournament.db not found. Run from C:\\MLBB TOURNAMENT")

names = [
    "Bagwis Esports", "Парламент", "Я и 4 крипа", "Ne igroki",
    "Поколение чудес", "Abyss Warriors", "GROM", "KPI",
    "Додики", "Team Surrender", "Фаны СерёжиБоже", "Ирис", "Nyashki",
]

con = sqlite3.connect(DB)
cur = con.cursor()
tid = cur.execute("SELECT id FROM tournaments WHERE status='active'").fetchone()
if not tid:
    cur.execute("INSERT INTO tournaments (status) VALUES ('active')")
    tid = cur.execute("SELECT id FROM tournaments WHERE status='active'").fetchone()
tid = tid[0]

# fake user ids starting from 9000000001
base_uid = 9000000001
for i, name in enumerate(names):
    uid = base_uid + i
    norm = name.lower().replace(" ", "")[:20]
    cur.execute(
        "INSERT OR IGNORE INTO users (telegram_id, username, full_name) VALUES (?,?,?)",
        (uid, f"test{i}", name),
    )
    cur.execute(
        "INSERT INTO teams (name, name_norm, created_by, captain_id, tournament_id) VALUES (?,?,?,?,?)",
        (name, norm, uid, uid, tid),
    )
    team_id = cur.lastrowid
    cur.execute(
        "INSERT INTO team_members (team_id, user_id, is_captain) VALUES (?,?,1)",
        (team_id, uid),
    )
    print(f"added: {name}")

con.commit()
con.close()
print("done")
