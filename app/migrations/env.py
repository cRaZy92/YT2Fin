"""Alembic environment.

The app runs migrations in code (app.db.init_db) and passes its connection in. The CLI
(`alembic revision --autogenerate`) connects to CONFIG_DIR/app.db (default ./config).
"""
from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (registers tables)
from app.db import Base

target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    context.configure(target_metadata=target_metadata, render_as_batch=True, compare_type=True, **kwargs)


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url")
    if url:
        return url
    return f"sqlite:///{Path(os.environ.get('CONFIG_DIR', 'config')) / 'app.db'}"


def run_offline() -> None:
    _configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url())
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
