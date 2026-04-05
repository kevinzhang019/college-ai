"""Database connection and session management for admissions data."""

import os
import time
import logging
import threading
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Suppress noisy SQLAlchemy "Exception during reset" logs for dead Hrana streams
logging.getLogger("sqlalchemy.pool.impl").setLevel(logging.CRITICAL)

# Load env from project root
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
load_dotenv(os.path.join(_project_root, ".env"))

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# ---------------------------------------------------------------------------
# Engine factory — recreating the engine forces the libsql driver to open a
# fresh WebSocket, which is the only reliable way to recover from Hrana
# stream expiry during long scraping sessions.
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_engine = None
_session_factory = None


def _make_turso_engine():
    _host = TURSO_DATABASE_URL.replace("libsql://", "")
    return create_engine(
        f"sqlite+libsql://{_host}?secure=true",
        connect_args={"auth_token": TURSO_AUTH_TOKEN},
        echo=False,
        poolclass=NullPool,
    )


def _make_local_engine():
    _data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data")
    os.makedirs(_data_dir, exist_ok=True)
    db_path = os.getenv("ADMISSIONS_DB_PATH", os.path.join(_data_dir, "admissions.db"))
    return create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )


def _init_engine():
    global _engine, _session_factory
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        _engine = _make_turso_engine()
    else:
        _engine = _make_local_engine()
    _session_factory = sessionmaker(bind=_engine)


def reset_engine():
    """Drop the current engine and create a fresh one (new WebSocket).

    Builds both the new engine and session factory into locals before
    swapping globals, so a partial failure can't leave _engine and
    _session_factory pointing to different engines.
    """
    global _engine, _session_factory
    with _engine_lock:
        old = _engine
        # Build both into locals first — if either fails, globals stay consistent
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            new_engine = _make_turso_engine()
        else:
            new_engine = _make_local_engine()
        new_factory = sessionmaker(bind=new_engine)
        # Atomic swap: both globals update together
        _engine = new_engine
        _session_factory = new_factory
        logger.info("Engine reset: created fresh Turso connection")
        if old is not None:
            try:
                old.dispose()
            except Exception:
                pass


_init_engine()


# ---------------------------------------------------------------------------
# Hrana / Turso resilience
# ---------------------------------------------------------------------------

def is_hrana_error(exc: Exception) -> bool:
    """Detect Turso Hrana WebSocket stream expiry errors."""
    msg = str(exc).lower()
    return any(p in msg for p in [
        "stream not found", "stream expired", "hrana", "websocket",
    ])


def get_session() -> Session:
    with _engine_lock:
        factory = _session_factory
        return factory()


def get_engine():
    """Return the current engine under ``_engine_lock``.

    Matches the lock discipline of ``get_session()`` so that concurrent
    ``reset_engine()`` calls cannot invalidate the reference mid-use.
    """
    with _engine_lock:
        return _engine


def with_retry(work_fn, max_retries=3):
    """Execute a unit of work with automatic retry on Hrana stream expiry.

    Creates a fresh session per attempt and calls ``work_fn(session)``.
    The session is committed on success and rolled back + closed on failure.
    On Hrana errors the engine is reset to force a new WebSocket connection.
    """
    for attempt in range(max_retries):
        session = get_session()
        try:
            result = work_fn(session)
            session.commit()
            return result
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            if is_hrana_error(e) and attempt < max_retries - 1:
                delay = 0.5 * (2 ** attempt)
                logger.warning(
                    "Hrana error (attempt %d/%d), resetting engine, retry in %.1fs: %s",
                    attempt + 1, max_retries, delay, e,
                )
                reset_engine()
                time.sleep(delay)
                continue
            raise
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Schema initialisation & migrations
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables and run lightweight migrations for new columns."""
    from college_ai.db.models import Base
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate_add_columns(engine)
    _migrate_drop_columns(engine)


def _migrate_add_columns(engine):
    """Add columns that create_all() won't add to existing tables."""
    from sqlalchemy import inspect
    insp = inspect(engine)

    migrations = {
        "schools": [("yield_rate", "FLOAT")],
        "applicant_datapoints": [("major", "TEXT")],
        "niche_grades": [("no_data", "INTEGER DEFAULT 0")],
    }

    with engine.connect() as conn:
        for table, columns in migrations.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col_name, col_type in columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                    conn.commit()


def _migrate_drop_columns(engine):
    """Drop columns removed from models (requires SQLite >= 3.35.0)."""
    from sqlalchemy import inspect
    insp = inspect(engine)

    drops = {
        "applicant_datapoints": ["gender", "hs_state", "hs_type", "decision_type", "applicant_type"],
    }

    with engine.connect() as conn:
        for table, columns in drops.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col_name in columns:
                if col_name in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col_name}"))
                        conn.commit()
                    except Exception as e:
                        logger.warning(f"Could not drop {table}.{col_name}: {e}")
