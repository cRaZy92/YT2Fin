from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import db
from app.db import Base, init_db, make_engine
from app.models import Mapping, Video


def tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_fresh_database_is_migrated_to_head(tmp_path):
    engine = make_engine(tmp_path / "app.db")
    init_db(engine, tmp_path / "backups")
    assert {"mappings", "videos", "season_breaks", "ignored_videos", "settings", "alembic_version"} <= tables(engine)
    assert db.current_revision(engine) == db.head_revision()
    assert not (tmp_path / "backups").exists()


def test_migrations_match_the_models(tmp_path):
    """Fails when a model changed without a migration: run `alembic revision --autogenerate`."""
    engine = make_engine(tmp_path / "app.db")
    init_db(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def make_legacy_database(engine) -> None:
    """The schema create_all produced before migrations existed (= revision 0001), without a version row."""
    from alembic import command

    config = db.alembic_config()
    with db.migration_connection(engine) as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, db.BASELINE_REVISION)
        conn.exec_driver_sql("DROP TABLE alembic_version")


def test_legacy_create_all_database_is_adopted_without_data_loss(tmp_path):
    engine = make_engine(tmp_path / "app.db")
    make_legacy_database(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO mappings (name, type, folder, enabled, overrides, created_at) "
            "VALUES ('Old', 'show', '/media/old', 1, '{}', '2026-01-01 00:00:00')"
        )
    init_db(engine, tmp_path / "backups")
    assert db.current_revision(engine) == db.head_revision()
    with engine.connect() as conn:
        assert conn.execute(sa.select(Mapping.name)).scalars().all() == ["Old"]


def test_init_db_is_idempotent(tmp_path):
    engine = make_engine(tmp_path / "app.db")
    init_db(engine)
    init_db(engine)
    assert db.current_revision(engine) == db.head_revision()


def test_should_backup_only_when_pending_and_there_is_data():
    assert db.should_backup(current="0001", head="0002", has_tables=True)
    assert db.should_backup(current=None, head="0002", has_tables=True)  # legacy database
    assert not db.should_backup(current="0002", head="0002", has_tables=True)
    assert not db.should_backup(current=None, head="0002", has_tables=False)  # brand new


def test_backup_database_copies_and_keeps_the_newest_five(tmp_path):
    engine = make_engine(tmp_path / "app.db")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(sa.insert(Mapping).values(name="Keep", type="show", folder="/media/k", overrides={}))
    backups = tmp_path / "backups"
    made = [db.backup_database(tmp_path / "app.db", backups, f"rev{i}") for i in range(7)]
    remaining = sorted(backups.iterdir())
    assert len(remaining) == 5 and made[-1] in remaining and made[0] not in remaining
    copy = make_engine(made[-1])
    with copy.connect() as conn:
        assert conn.execute(sa.select(Mapping.name)).scalars().all() == ["Keep"]


def test_batch_table_rebuild_during_migration_keeps_child_rows(tmp_path):
    """SQLite rebuilds tables to alter them; with foreign keys on, dropping `mappings` would cascade-delete videos."""
    engine = make_engine(tmp_path / "app.db")
    init_db(engine)
    with engine.begin() as conn:
        mapping_id = conn.execute(
            sa.insert(Mapping).values(name="M", type="show", folder="/media/m", overrides={})
        ).inserted_primary_key[0]
        conn.execute(sa.insert(Video).values(mapping_id=mapping_id, youtube_id="v", position=1))
    with db.migration_connection(engine) as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
        op = Operations(MigrationContext.configure(conn))
        with op.batch_alter_table("mappings", recreate="always") as batch:
            batch.add_column(sa.Column("extra", sa.Integer, nullable=True))
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(Video)).scalar() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_migration_scripts_ship_inside_the_package():
    scripts = Path(db.__file__).parent / "migrations"
    assert (scripts / "env.py").is_file() and (scripts / "script.py.mako").is_file()
    assert list((scripts / "versions").glob("*.py"))


def test_0002_adds_include_shorts_defaulting_to_false_for_existing_mappings(tmp_path):
    from alembic import command

    engine = make_engine(tmp_path / "app.db")
    config = db.alembic_config()
    with db.migration_connection(engine) as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0001")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO mappings (name, type, folder, enabled, overrides, created_at) "
            "VALUES ('Old', 'show', '/media/old', 1, '{}', '2026-01-01 00:00:00')"
        )
    init_db(engine, tmp_path / "backups")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT include_shorts FROM mappings").scalar() == 0
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_cli_upgrade_without_app_connection_records_the_revision(tmp_path):
    """The developer CLI path (`alembic upgrade`) must commit the version row, not just the DDL."""
    from alembic import command

    config = db.alembic_config()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'cli.db'}")
    command.upgrade(config, "head")
    assert db.current_revision(make_engine(tmp_path / "cli.db")) == db.head_revision()


def test_a_failing_migration_leaves_no_partial_schema_changes(tmp_path):
    import pytest

    engine = make_engine(tmp_path / "app.db")
    init_db(engine)
    with pytest.raises(RuntimeError):
        with db.migration_connection(engine) as conn:
            op = Operations(MigrationContext.configure(conn))
            op.create_table("half_done", sa.Column("id", sa.Integer, primary_key=True))
            with op.batch_alter_table("mappings") as batch:
                batch.add_column(sa.Column("half_col", sa.Integer, nullable=True))
            raise RuntimeError("migration blew up")
    assert "half_done" not in tables(engine)
    assert "half_col" not in {c["name"] for c in sa.inspect(engine).get_columns("mappings")}
