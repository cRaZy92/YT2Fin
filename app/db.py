from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"  # schema that create_all produced before migrations existed
BACKUPS_KEPT = 5


class Base(DeclarativeBase):
    pass


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config():
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    from alembic.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def should_backup(*, current: str | None, head: str, has_tables: bool) -> bool:
    return has_tables and current != head


@contextmanager
def migration_connection(engine: Engine) -> Iterator[Connection]:
    """A transaction with foreign keys OFF.

    SQLite alters tables by rebuilding them; with foreign keys on, dropping the old parent
    table would cascade-delete every child row. The pragma only works outside a transaction.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()  # end SQLAlchemy's autobegun transaction so we can open our own
        try:
            with conn.begin():
                yield conn
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()


def backup_database(db_path: Path, backups_dir: Path, label: str, keep: int = BACKUPS_KEPT) -> Path:
    """Consistent copy via SQLite's backup API (safe with WAL), keeping the newest `keep` copies."""
    from app.timeutil import utcnow

    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"app.db.{utcnow():%Y%m%d-%H%M%S-%f}.{label}"
    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
    for old in sorted(backups_dir.glob("app.db.*"))[:-keep]:
        old.unlink()
    return target


def init_db(engine: Engine, backups_dir: Path | None = None) -> None:
    """Bring the schema to the latest migration, adopting pre-migration databases."""
    from alembic import command

    from app import models  # noqa: F401  (registers tables on Base.metadata)

    head = head_revision()
    current = current_revision(engine)
    has_tables = bool(set(inspect(engine).get_table_names()) - {"alembic_version"})
    if backups_dir is not None and should_backup(current=current, head=head, has_tables=has_tables):
        path = backup_database(Path(engine.url.database), backups_dir, current or "pre-migrations")
        log.info("Backed up the database to %s before migrating", path)
    config = alembic_config()
    with migration_connection(engine) as conn:
        config.attributes["connection"] = conn
        if current is None and has_tables:
            log.info("Adopting a database created before migrations existed (revision %s)", BASELINE_REVISION)
            command.stamp(config, BASELINE_REVISION)
        if current != head:
            log.info("Migrating database from %s to %s", current or "empty", head)
        command.upgrade(config, "head")
