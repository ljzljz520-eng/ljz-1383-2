"""Runtime configuration.

Storage paths / admin password can be overridden via env vars (tests do this).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'storage' / 'app.db'}")
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", BASE_DIR / "storage" / "files"))

# Seeded admin credentials. In production this would come from a secret store.
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "studio2026")

# Cookie session signing
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Schedule: a slot is "holding" (temporary hold) until booked; holds expire.
HOLD_TTL_HOURS = int(os.environ.get("HOLD_TTL_HOURS", "48"))
