"""Database connection and session management for admissions data."""

import os
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

# Load env from project root
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
load_dotenv(os.path.join(_project_root, ".env"))

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
    _host = TURSO_DATABASE_URL.replace("libsql://", "")
    ENGINE = create_engine(
        f"sqlite+libsql://{_host}?secure=true",
        connect_args={"auth_token": TURSO_AUTH_TOKEN},
        echo=False,
        poolclass=NullPool,  # fresh connection per session — avoids Hrana stream expiry
        pool_pre_ping=True,  # verify connection is alive before use
    )
else:
    _data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(_data_dir, exist_ok=True)
    DB_PATH = os.getenv("ADMISSIONS_DB_PATH", os.path.join(_data_dir, "admissions.db"))
    ENGINE = create_engine(
        f"sqlite:///{DB_PATH}",
        echo=False,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(bind=ENGINE)


def get_session() -> Session:
    return SessionLocal()


def init_db():
    """Create all tables and run lightweight migrations for new columns."""
    from preference_scraper.admissions.models import Base
    Base.metadata.create_all(ENGINE)
    _migrate_add_columns()


def _migrate_add_columns():
    """Add columns that create_all() won't add to existing tables."""
    from sqlalchemy import text, inspect
    insp = inspect(ENGINE)

    # Map of (table_name -> list of (column_name, column_type))
    migrations = {
        "schools": [("yield_rate", "FLOAT")],
    }

    with ENGINE.connect() as conn:
        for table, columns in migrations.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col_name, col_type in columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                    conn.commit()
