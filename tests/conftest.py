import importlib.util
import sqlite3 as _sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None, f"cannot load {relpath}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_orders_db(db_path: str) -> None:
    """Apply the state-001 + state-002 + state-003 schema to a fresh
    SQLite file. Mirrors the DDL the orchestrator's `state-001-orders`,
    `state-002-email-feedback`, and `state-003-email-feedback-schema-version`
    migrations produce, so these tests stay tied to the real schema rather
    than a divergent fixture shape. The orders cluster shares the
    orchestrator's `messages.db` (mounted rw on main/trusted) per
    jbaruch/nanoclaw container-runner."""
    conn = _sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE orders (
              id                TEXT PRIMARY KEY,
              source            TEXT NOT NULL,
              status            TEXT NOT NULL,
              amount            REAL,
              currency          TEXT,
              description       TEXT NOT NULL,
              order_date        TEXT NOT NULL,
              expected_delivery TEXT,
              email_message_id  TEXT NOT NULL UNIQUE,
              to_address        TEXT,
              flagged           INTEGER NOT NULL DEFAULT 0,
              flag_reason       TEXT,
              last_updated      TEXT NOT NULL
            );
            CREATE INDEX idx_orders_source_status ON orders(source, status);
            CREATE INDEX idx_orders_order_date ON orders(order_date);
            CREATE TABLE orders_metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE email_feedback (
              id             INTEGER PRIMARY KEY AUTOINCREMENT,
              pattern        TEXT NOT NULL,
              label          TEXT NOT NULL CHECK(label IN ('actionable', 'noise')),
              source         TEXT NOT NULL DEFAULT 'baruch-response',
              date           TEXT NOT NULL,
              created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              schema_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX idx_email_feedback_pattern ON email_feedback(pattern);
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def apply_order(tmp_path, monkeypatch):
    """Load check-orders/scripts/apply-order.py with DB_PATH pointing
    at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "apply_order_under_test",
        "skills/check-orders/scripts/apply-order.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def write_orders_metadata(tmp_path, monkeypatch):
    """Load check-orders/scripts/write-orders-metadata.py with DB_PATH
    pointing at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "write_orders_metadata_under_test",
        "skills/check-orders/scripts/write-orders-metadata.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def unflag_orders(tmp_path, monkeypatch):
    """Load check-orders/scripts/unflag-orders.py with DB_PATH pointing
    at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "unflag_orders_under_test",
        "skills/check-orders/scripts/unflag-orders.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def get_flagged_orders(tmp_path, monkeypatch):
    """Load check-orders/scripts/get-flagged-orders.py with DB_PATH pointing
    at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "get_flagged_orders_under_test",
        "skills/check-orders/scripts/get-flagged-orders.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def compute_order_id():
    """Load check-orders/scripts/compute-order-id.py.

    Pure function of (source, order_date, description) — no module-level
    state to redirect. Tests cover the SHA-1-prefix contract end-to-end.
    """
    return _load(
        "compute_order_id_under_test",
        "skills/check-orders/scripts/compute-order-id.py",
    )


@pytest.fixture
def fetch_order_emails(tmp_path, monkeypatch):
    """Load check-orders/scripts/fetch-order-emails.py with the sanitizer
    mount + fallback redirected at a tmp_path file (NOT created, so the
    fail-closed `main()` path is exercisable) and DB_PATH redirected at a
    tmp_path-rooted SQLite file seeded with the orders fixture.
    Returned tuple is (module, sanitizer_path, db_path).

    The pure `fetch_order_emails(execute, sanitize_message, queries)` core
    is tested directly with an injected fake `execute` + the
    `sanitize_email_body` stub, so it needs no network and no real
    heartbeat sibling."""
    sanitizer_path = tmp_path / "sanitize-email-body.py"
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "fetch_order_emails_under_test",
        "skills/check-orders/scripts/fetch-order-emails.py",
    )
    monkeypatch.setattr(module, "SANITIZER_MOUNT", str(sanitizer_path))
    monkeypatch.setattr(module, "SANITIZER_FALLBACK", sanitizer_path)
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, sanitizer_path, db_path


@pytest.fixture
def flag_anomalies(tmp_path, monkeypatch):
    """Load check-orders/scripts/flag-anomalies.py with DB_PATH pointing
    at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "flag_anomalies_under_test",
        "skills/check-orders/scripts/flag-anomalies.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def promote_stale_shipped(tmp_path, monkeypatch):
    """Load check-orders/scripts/promote-stale-shipped.py with DB_PATH
    pointing at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "promote_stale_shipped_under_test",
        "skills/check-orders/scripts/promote-stale-shipped.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def read_last_checked(tmp_path, monkeypatch):
    """Load check-orders/scripts/read-last-checked.py with DB_PATH pointing
    at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "read_last_checked_under_test",
        "skills/check-orders/scripts/read-last-checked.py",
    )
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, db_path


@pytest.fixture
def within_days():
    """Load check-orders/scripts/within-days.py.

    Script imports `from datetime import date` at module level, so tests
    that need a fixed today() pin `module.date` to a frozen subclass.
    """
    return _load(
        "within_days_under_test",
        "skills/check-orders/scripts/within-days.py",
    )


class _SanitizerStub:
    """Identity test double for the heartbeat skill's sanitize-email-body.py.

    fetch-order-emails.py loads the real sanitizer at runtime from the
    co-loaded `tessl__heartbeat` tile mount; this tile does not ship it.
    The orders-owned projection/dedup/fallback logic only requires a
    callable `sanitize_message`, so an identity double exercises it
    without coupling the suite to heartbeat's internals. The sanitizer's
    own behavior (body cap, invisible-unicode collapse) is tested in
    jbaruch/nanoclaw-admin's heartbeat suite. Revisit when the Gmail
    fetch path is rewritten under jbaruch/nanoclaw#639."""

    @staticmethod
    def sanitize_message(msg):
        return msg

    @staticmethod
    def sanitize(text, max_len=2000):
        return text


@pytest.fixture
def sanitize_email_body():
    return _SanitizerStub()
