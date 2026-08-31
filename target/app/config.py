"""Runtime settings, sourced from the environment with safe local defaults."""

import os

DATABASE_PATH = os.environ.get("SHORTENER_DB_PATH", "shortener.db")
BASE_URL = os.environ.get("SHORTENER_BASE_URL", "http://localhost:8000")
RATE_LIMIT_PER_MINUTE = int(os.environ.get("SHORTENER_RATE_LIMIT_PER_MINUTE", "30"))
MIN_CODE_LENGTH = 6
