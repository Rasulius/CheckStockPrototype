from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_DATABASE_URL = "postgresql+psycopg://checkstock:checkstock@localhost:5433/checkstock"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
IS_SQLITE = DATABASE_URL.startswith("sqlite")
