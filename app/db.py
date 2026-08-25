"""MySQL / MariaDB access layer (PyMySQL).

Everything persists to the customer's MySQL database (managed via phpMyAdmin). One
process, several worker threads (poller / ticker / outbox) plus Flask request threads —
MySQL connections are not shared across threads, so each thread gets its own connection
(thread-local). Connections run with autocommit ON so the long-lived worker threads
always read the latest committed rows; writes use explicit transactions.

To keep the domain code database-agnostic and readable, a thin CursorProxy lets callers
keep the sqlite-style conventions used throughout core/:
  * "?" placeholders (translated to PyMySQL's "%s"),
  * chainable execute:  cur = c.execute(...); cur.fetchone(); cur.lastrowid,
  * an optional table-name prefix applied centrally.
No ORM. The domain modules speak SQL.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

_local = threading.local()
_params: dict | None = None
_prefix: str = ""
_write_lock = threading.Lock()  # serialise writers within this process (Rule 3 posture)

# Known table names, for the optional prefix rewrite. Word-boundary matching means
# column names like `incident_id` / `matched_ticket_id` are never touched.
_TABLES = [
    "schema_migrations", "incident_reasons", "inbound_raw", "config_overrides",
    "assets", "incidents", "tickets", "escalations", "outbox", "events",
    "asset_offplan",
]
_TABLE_RE = {t: re.compile(r"\b" + t + r"\b") for t in _TABLES}

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

log = logging.getLogger("ops.db")

# MySQL error codes meaning "this DDL is already in place". Re-running a migration that
# hits one of these is a no-op, not a failure.
_ALREADY_APPLIED = {
    1050,  # table already exists
    1060,  # duplicate column name
    1061,  # duplicate key name
    1091,  # can't DROP; check that column/key exists
    1826,  # duplicate foreign key constraint name
}


def init(params: dict, table_prefix: str = "") -> None:
    """Point the layer at a MySQL server and run migrations. Call once at startup.

    params: host, port, user, password, database (charset defaulted to utf8mb4).
    """
    global _params, _prefix
    _params = dict(params)
    _params.setdefault("charset", "utf8mb4")
    _prefix = table_prefix or ""
    migrate()


# MySQL scopes FOREIGN KEY and CHECK constraint names to the DATABASE, not the table.
# Two prefixed installs in one database therefore collide on `fk_inc_asset` even though
# their tables are cleanly separated — and the collision surfaces as error 1826, which
# migrate() treats as "already applied" and skips. The CREATE TABLE is silently dropped,
# and the next table's foreign key to it fails with a completely unrelated 1824.
#
# Prefixing constraint names too is what makes OPS_TABLE_PREFIX actually deliver the
# thing it exists for: more than one ops-core sharing the plant's database.
_CONSTRAINT_RE = re.compile(r"\bCONSTRAINT\s+(`?)(\w+)\1", re.IGNORECASE)


def _apply_prefix(sql: str) -> str:
    if not _prefix:
        return sql
    for t, rx in _TABLE_RE.items():
        sql = rx.sub(_prefix + t, sql)
    sql = _CONSTRAINT_RE.sub(
        lambda m: f"CONSTRAINT {m.group(1)}{_prefix}{m.group(2)}{m.group(1)}"
        if not m.group(2).startswith(_prefix) else m.group(0),
        sql,
    )
    return sql


def _translate_placeholders(sql: str) -> str:
    """Translate '?' placeholders to PyMySQL's '%s', but only OUTSIDE single-quoted
    string literals — so a literal like COALESCE(shift,'?') is left intact. (SQL in this
    codebase never contains a literal '%'.)"""
    out: list[str] = []
    in_quote = False
    for ch in sql:
        if ch == "'":
            in_quote = not in_quote
            out.append(ch)
        elif ch == "?" and not in_quote:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _prepare(sql: str) -> str:
    return _translate_placeholders(_apply_prefix(sql))


def _new_connection() -> pymysql.connections.Connection:
    if _params is None:
        raise RuntimeError("db.init() must be called before using the database")
    return pymysql.connect(
        host=_params.get("host", "127.0.0.1"),
        port=int(_params.get("port", 3306)),
        user=_params.get("user"),
        password=_params.get("password", ""),
        database=_params.get("database"),
        charset=_params.get("charset", "utf8mb4"),
        autocommit=True,
        cursorclass=DictCursor,
    )


def conn() -> pymysql.connections.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _new_connection()
        _local.conn = c
        return c
    try:
        c.ping(reconnect=False)  # cheap liveness check; raises if the link dropped
    except Exception:            # heal dropped connections (idle worker threads)
        try:
            c.close()
        except Exception:
            pass
        c = _new_connection()
        _local.conn = c
    return c


class CursorProxy:
    """Wraps a PyMySQL cursor to preserve the sqlite-style API used in core/."""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params=()):
        self._cur.execute(_prepare(sql), params if params else None)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with conn().cursor() as cur:
        cur.execute(_prepare(sql), params if params else None)
        return list(cur.fetchall())


def query_one(sql: str, params: tuple | dict = ()) -> dict | None:
    with conn().cursor() as cur:
        cur.execute(_prepare(sql), params if params else None)
        return cur.fetchone()


@contextmanager
def transaction():
    """Serialised write transaction. Yields a CursorProxy.

        with db.transaction() as c:
            c.execute("INSERT ...", (...))
    Commits on success, rolls back on exception.
    """
    c = conn()
    with _write_lock:
        c.begin()
        cur = c.cursor()
        try:
            yield CursorProxy(cur)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            cur.close()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Single-statement write. Returns lastrowid."""
    with transaction() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


# --- migrations ------------------------------------------------------------

def _split_statements(sql: str) -> list[str]:
    out, buf = [], []
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).rstrip().rstrip(";").strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def migrate(migrations_dir: str | None = None) -> list[str]:
    """Apply any *.sql migrations not yet recorded. Returns names applied."""
    from . import clock

    mdir = Path(migrations_dir) if migrations_dir else _MIGRATIONS_DIR
    c = conn()
    with c.cursor() as cur:
        cur.execute(_prepare(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name VARCHAR(191) NOT NULL, applied_at CHAR(25) NOT NULL,"
            " PRIMARY KEY (name)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        ))
        cur.execute(_prepare("SELECT name FROM schema_migrations"))
        applied = {r["name"] for r in cur.fetchall()}

    ran: list[str] = []
    for path in sorted(mdir.glob("*.sql")):
        if path.name in applied:
            continue
        statements = _split_statements(path.read_text(encoding="utf-8"))
        with c.cursor() as cur:
            for stmt in statements:
                try:
                    cur.execute(_prepare(stmt))
                except pymysql.err.OperationalError as exc:
                    # MySQL commits each DDL statement implicitly, so a migration can
                    # never be atomic: a run that dies partway leaves some statements
                    # applied and the bookkeeping row unwritten, and the next boot
                    # replays the file. CREATE TABLE handles that itself via IF NOT
                    # EXISTS, but MySQL 8 has no ADD COLUMN IF NOT EXISTS (MariaDB
                    # does; we cannot rely on it). Treating "already applied" as
                    # success keeps every statement type re-runnable, which is the
                    # property the whole recovery story depends on.
                    if exc.args and exc.args[0] in _ALREADY_APPLIED:
                        log.info("migration %s: already applied — %s",
                                 path.name, exc.args[1])
                        continue
                    raise
            cur.execute(
                _prepare("INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)"),
                (path.name, clock.now_iso()),
            )
        ran.append(path.name)
    return ran


def reset_all() -> None:
    """DROP every ops-core table. Test/dev only — never call in production."""
    c = conn()
    with c.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in _TABLES:
            cur.execute(_prepare(f"DROP TABLE IF EXISTS {t}"))
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
