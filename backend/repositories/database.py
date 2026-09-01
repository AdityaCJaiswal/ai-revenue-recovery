"""MySQL connection and schema migrations.

Engine decision: MySQL (team choice — see docs/DATA_MODEL.md for the trade-off
notes). Two footguns are neutralised here by construction rather than hoped
away:

- **Session timezone.** Every connection runs `SET time_zone = '+00:00'`. All
  DATETIME values in this database are UTC, always. Conversion to Asia/Kolkata
  happens at the edge (the RBI contact-window gate), never in the database — a
  session-tz change can therefore never alter whether an action was legal.
- **Charset.** utf8mb4 everywhere: verbatim Hinglish/Devanagari customer speech
  lands in these tables (promises_to_pay.verbatim, voice_utterances).

Migrations are plain numbered .sql files in ./migrations, tracked in
schema_migrations, applied once at startup — not per request.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pymysql
from pymysql.cursors import DictCursor

from ..core.config import get_settings
from ..core.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _connect(database: str | None) -> pymysql.connections.Connection:
    s = get_settings()
    return pymysql.connect(
        host=s.mysql_host,
        port=s.mysql_port,
        user=s.mysql_user,
        password=s.mysql_password,
        database=database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        init_command="SET time_zone = '+00:00'",  # UTC by construction
        autocommit=False,
    )


def connect(database: str | None = None) -> pymysql.connections.Connection:
    """Connection to the app database. ensure_schema() must have run once."""
    return _connect(database or get_settings().mysql_database)


@contextmanager
def session(database: str | None = None) -> Iterator[pymysql.connections.Connection]:
    # ponytail: one connection per request, no pool. Add pooling when latency
    # under load matters; locally this costs ~1ms.
    conn = connect(database)
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema(database: str | None = None) -> None:
    """Create the database if missing and apply pending migrations. Idempotent."""
    db = database or get_settings().mysql_database

    server = _connect(None)
    try:
        with server.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        server.commit()
    finally:
        server.close()

    conn = _connect(db)
    try:
        _run_migrations(conn)
    finally:
        conn.close()


def _run_migrations(conn: pymysql.connections.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename   VARCHAR(255) PRIMARY KEY,"
            "  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6))"
        )
        conn.commit()
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {row["filename"] for row in cur.fetchall()}

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        # Strip -- comments BEFORE splitting on ';' -- a semicolon inside a
        # comment already broke this once. Safe while no string literal
        # contains '--'; revisit if seed-data migrations appear.
        sql = "\n".join(line.split("--", 1)[0] for line in path.read_text().splitlines())
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
        conn.commit()
        log.info("applied migration %s (%d statements)", path.name, len(statements))
