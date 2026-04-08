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


def is_blocked_error(exc: Exception) -> bool:
    """Detect Turso plan-level blocks (quota exhaustion).

    When monthly row-read/write quotas are exceeded, Turso returns a BLOCKED
    error code.  These are NOT transient — retrying or resetting the engine
    will not help.  Callers should fail fast.
    """
    msg = str(exc).lower()
    return "blocked" in msg and "upgrade" in msg


def get_session() -> Session:
    with _engine_lock:
        factory = _session_factory
    # Call factory() OUTSIDE the lock — factory() opens a Turso WebSocket
    # connection (network I/O) which can be slow.  Holding _engine_lock
    # during that I/O would stall concurrent reset_engine() calls.
    # The local `factory` reference stays valid even if reset_engine()
    # swaps _session_factory immediately after: with NullPool, sessions
    # from the old engine use their own independent connections.
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
            if is_blocked_error(e):
                raise  # Quota block is not transient — fail fast
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

    # Rename old flat column names → category-prefixed names (libSQL/SQLite 3.25+)
    school_renames = [
        ("acceptance_rate", "admissions_rate"),
        ("sat_avg", "admissions_sat_avg"),
        ("sat_25", "admissions_sat_25"),
        ("sat_75", "admissions_sat_75"),
        ("act_25", "admissions_act_25"),
        ("act_75", "admissions_act_75"),
        ("enrollment", "student_size"),
        ("retention_rate", "student_retention_rate"),
        ("student_faculty_ratio", "student_faculty_ratio"),  # same name, no-op
        ("graduation_rate", "outcome_graduation_rate"),
        ("median_earnings_10yr", "outcome_median_earnings_10yr"),
        ("tuition_in_state", "cost_tuition_in_state"),
        ("tuition_out_of_state", "cost_tuition_out_of_state"),
        ("pct_white", "student_pct_white"),
        ("pct_black", "student_pct_black"),
        ("pct_hispanic", "student_pct_hispanic"),
        ("pct_asian", "student_pct_asian"),
        ("pct_first_gen", "student_pct_first_gen"),
    ]

    if insp.has_table("schools"):
        existing = {c["name"] for c in insp.get_columns("schools")}
        with engine.connect() as conn:
            for old_name, new_name in school_renames:
                if old_name == new_name:
                    continue
                if old_name in existing and new_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE schools RENAME COLUMN {old_name} TO {new_name}"
                    ))
                    conn.commit()
            # Drop yield_rate (Scorecard API doesn't have this field)
            if "yield_rate" in existing:
                conn.execute(text("ALTER TABLE schools DROP COLUMN yield_rate"))
                conn.commit()

    # New columns to add across tables
    migrations = {
        "schools": [
            ("identity_alias", "TEXT"),
            ("identity_url", "TEXT"),
            ("identity_locale", "INTEGER"),
            ("identity_carnegie_basic", "INTEGER"),
            ("identity_religious_affiliation", "INTEGER"),
            ("admissions_test_requirements", "INTEGER"),
            ("student_avg_age_entry", "INTEGER"),
            ("student_pct_men", "FLOAT"),
            ("student_pct_women", "FLOAT"),
            ("student_part_time_share", "FLOAT"),
            ("cost_attendance", "INTEGER"),
            ("cost_avg_net_price", "INTEGER"),
            ("cost_booksupply", "INTEGER"),
            ("cost_net_price_0_30k", "INTEGER"),
            ("cost_net_price_30_48k", "INTEGER"),
            ("cost_net_price_48_75k", "INTEGER"),
            ("cost_net_price_75_110k", "INTEGER"),
            ("cost_net_price_110k_plus", "INTEGER"),
            ("aid_pell_grant_rate", "FLOAT"),
            ("aid_federal_loan_rate", "FLOAT"),
            ("aid_median_debt", "FLOAT"),
            ("aid_cumulative_debt_25th", "FLOAT"),
            ("aid_cumulative_debt_75th", "FLOAT"),
            ("institution_endowment", "INTEGER"),
            ("institution_faculty_salary", "INTEGER"),
            ("institution_ft_faculty_rate", "FLOAT"),
            ("institution_instructional_spend_per_fte", "INTEGER"),
        ],
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
