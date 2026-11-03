"""
One-time data migration: copies rows from a SQLite backup file into the
live PostgreSQL database, in FK-safe order (users -> api_keys/model_records/
webhooks -> inference_logs).

SQLite here never enabled `PRAGMA foreign_keys`, so rows with dangling FK
references (an api_key pointing at a deleted user, etc.) may exist even
though they'd be rejected outright by Postgres. For each row, a dangling
reference is nulled out if the column is nullable, or the row is skipped
(and reported) if the column is NOT NULL.

Run once, after `alembic upgrade head` has created an empty schema in
Postgres. Re-running against a Postgres DB that already has these rows
will fail on the primary key — that's intentional, this is not an upsert.

Usage:
    python scripts/migrate_sqlite_to_postgres.py /path/to/backup.db
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import settings  # noqa: E402
from core.database import (  # noqa: E402
    APIKey,
    InferenceLog,
    ModelRecord,
    User,
    Webhook,
)


def _as_dict(obj) -> dict:
    """Column values keyed by Python attribute name (not DB column name —
    ModelRecord.metadata_ maps to DB column "metadata", which would collide
    with the ORM class's own `.metadata` registry if read positionally)."""
    mapper = sa_inspect(obj).mapper
    return {attr.key: getattr(obj, attr.key) for attr in mapper.column_attrs}


async def _read_all(engine, model) -> list[dict]:
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        result = await session.execute(select(model))
        return [_as_dict(row) for row in result.scalars().all()]


async def _insert_all(engine, model, rows: list[dict]) -> int:
    if not rows:
        return 0
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        session.add_all([model(**row) for row in rows])
        await session.commit()
    return len(rows)


def _check_fk(rows, fk_field, valid_ids, nullable, orphans, table_name):
    """Null out or drop rows with a dangling FK reference; report each one."""
    clean = []
    for row in rows:
        ref = row.get(fk_field)
        if ref is not None and ref not in valid_ids:
            orphans.append(f"{table_name}.id={row['id']}: {fk_field}={ref!r} not found")
            if nullable:
                row[fk_field] = None
            else:
                continue  # NOT NULL column, dangling ref -- drop the row
        clean.append(row)
    return clean


async def migrate(sqlite_path: str) -> None:
    sqlite_url = f"sqlite+aiosqlite:///{sqlite_path}"
    sqlite_engine = create_async_engine(sqlite_url)
    pg_engine = create_async_engine(settings.DATABASE_URL)

    print(f"Reading from {sqlite_path} ...")
    users = await _read_all(sqlite_engine, User)
    api_keys = await _read_all(sqlite_engine, APIKey)
    model_records = await _read_all(sqlite_engine, ModelRecord)
    webhooks = await _read_all(sqlite_engine, Webhook)
    inference_logs = await _read_all(sqlite_engine, InferenceLog)
    await sqlite_engine.dispose()

    print(
        f"  users={len(users)} api_keys={len(api_keys)} "
        f"model_records={len(model_records)} webhooks={len(webhooks)} "
        f"inference_logs={len(inference_logs)}"
    )

    user_ids = {u["id"] for u in users}
    model_ids = {m["id"] for m in model_records}

    orphans: list[str] = []
    clean_api_keys = _check_fk(api_keys, "user_id", user_ids, False, orphans, "api_keys")
    clean_model_records = _check_fk(model_records, "owner_id", user_ids, True, orphans, "model_records")
    clean_webhooks = _check_fk(webhooks, "user_id", user_ids, False, orphans, "webhooks")
    clean_inference_logs = _check_fk(inference_logs, "user_id", user_ids, True, orphans, "inference_logs")
    clean_inference_logs = _check_fk(clean_inference_logs, "model_id", model_ids, False, orphans, "inference_logs")

    if orphans:
        print(f"\n{len(orphans)} dangling FK reference(s) found (SQLite never enforced FK constraints):")
        for line in orphans:
            print(f"  - {line}")
        print()

    print("Writing to Postgres ...")
    n_users = await _insert_all(pg_engine, User, users)
    n_api_keys = await _insert_all(pg_engine, APIKey, clean_api_keys)
    n_model_records = await _insert_all(pg_engine, ModelRecord, clean_model_records)
    n_webhooks = await _insert_all(pg_engine, Webhook, clean_webhooks)
    n_inference_logs = await _insert_all(pg_engine, InferenceLog, clean_inference_logs)
    await pg_engine.dispose()

    print(
        f"  users={n_users} api_keys={n_api_keys} "
        f"model_records={n_model_records} webhooks={n_webhooks} "
        f"inference_logs={n_inference_logs}"
    )
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} /path/to/backup.db", file=sys.stderr)
        sys.exit(1)
    asyncio.run(migrate(sys.argv[1]))
