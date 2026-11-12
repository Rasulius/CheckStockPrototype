#!/bin/sh
set -eu

python - <<'PY'
import os
import time

url = os.environ.get("DATABASE_URL", "")

if url.startswith("sqlite"):
    import os
    from pathlib import Path

    # sqlite:///./data/checkstock.db -> ./data/checkstock.db
    db_path = url.split("///", 1)[-1] if "///" in url else url.split(":///", 1)[-1]
    if db_path and db_path != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    from app.sqlite_init import init_sqlite_db

    init_sqlite_db()
    print("SQLite база инициализирована")
else:
    import psycopg

    pg_url = url.replace("postgresql+psycopg://", "postgresql://")
    for attempt in range(60):
        try:
            with psycopg.connect(pg_url, connect_timeout=3):
                print("PostgreSQL доступен")
                break
        except Exception as exc:  # noqa: BLE001
            print(f"Ждём PostgreSQL ({attempt + 1}/60): {exc}")
            time.sleep(1)
    else:
        raise SystemExit("PostgreSQL не поднялся за 60 секунд")

    import subprocess
    subprocess.check_call(["alembic", "upgrade", "head"])
PY

python seed.py
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
