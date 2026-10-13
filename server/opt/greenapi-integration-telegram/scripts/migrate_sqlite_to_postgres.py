#!/usr/bin/env python3
import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, delete, insert, select, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Base, DB_PATH, DATABASE_URL, engine


def _parse_args():
    parser = argparse.ArgumentParser(description="Copy local SQLite data/app.db into configured PostgreSQL DATABASE_URL.")
    parser.add_argument("--sqlite", default=str(DB_PATH), help="Source SQLite DB path")
    parser.add_argument("--force", action="store_true", help="Delete PostgreSQL table rows before import")
    return parser.parse_args()


def _parse_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return value
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(candidate.split(".")[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value
    if parsed.tzinfo:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _sqlite_rows(sqlite_path: str, table) -> list[dict]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        existing_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if table.name not in existing_tables:
            return []
        rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{table.name}"').fetchall()]
    finally:
        conn.close()

    valid_columns = {column.name: column for column in table.columns}
    converted = []
    for row in rows:
        item = {}
        for name, value in row.items():
            column = valid_columns.get(name)
            if column is None:
                continue
            if isinstance(column.type, DateTime):
                value = _parse_datetime(value)
            item[name] = value
        converted.append(item)
    return converted


async def _target_has_rows() -> bool:
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            count = (await conn.execute(select(table.c.id).limit(1))).first()
            if count:
                return True
    return False


async def _reset_sequence(conn, table):
    if "postgresql" not in DATABASE_URL:
        return
    if "id" not in table.c:
        return
    await conn.execute(text(
        "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
        "COALESCE((SELECT MAX(id) FROM " + table.name + "), 1), "
        "COALESCE((SELECT MAX(id) FROM " + table.name + "), 0) > 0)"
    ), {"table_name": table.name})


async def main():
    args = _parse_args()
    if "postgresql" not in DATABASE_URL:
        raise SystemExit(f"DATABASE_URL is not PostgreSQL: {DATABASE_URL}")
    if not Path(args.sqlite).exists():
        raise SystemExit(f"SQLite source not found: {args.sqlite}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if not args.force and await _target_has_rows():
        raise SystemExit("Target PostgreSQL already has rows. Use --force to replace them.")

    async with engine.begin() as conn:
        if args.force:
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(delete(table))

        imported = {}
        for table in Base.metadata.sorted_tables:
            rows = _sqlite_rows(args.sqlite, table)
            imported[table.name] = len(rows)
            if rows:
                for offset in range(0, len(rows), 500):
                    await conn.execute(insert(table), rows[offset:offset + 500])
            await _reset_sequence(conn, table)

    print({"database_url": "postgresql", "imported": imported})


if __name__ == "__main__":
    asyncio.run(main())
