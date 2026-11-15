"""The tests were written for PostgreSQL/asyncpg. On SQLite every constraint failure is a
sqlite3.IntegrityError, so the asyncpg exception names used by the tests map onto it."""
import sqlite3
from types import SimpleNamespace

asyncpg = SimpleNamespace(
    PostgresError=sqlite3.DatabaseError,
    UniqueViolationError=sqlite3.IntegrityError,
    CheckViolationError=sqlite3.IntegrityError,
    ForeignKeyViolationError=sqlite3.IntegrityError,
    RestrictViolationError=sqlite3.IntegrityError,
    NotNullViolationError=sqlite3.IntegrityError,
)
