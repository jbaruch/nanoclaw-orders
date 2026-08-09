import importlib.util
import sqlite3 as _sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relpath}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_orders_db(db_path: str) -> None:
    """Apply the state-001 + state-002 + state-003 + state-017 schema to a
    fresh SQLite file. Mirrors the DDL the orchestrator's `state-001-orders`,
    `state-002-email-feedback`, `state-003-email-feedback-schema-version`,
    and `state-017-orders-merchant-order-number` migrations produce, so these
    tests stay tied to the real schema rather than a divergent fixture shape.
    The orders cluster shares the orchestrator's `messages.db` (mounted rw on
    main/trusted) per jbaruch/nanoclaw container-runner."""
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
              last_updated      TEXT NOT NULL,
              merchant          TEXT,
              order_number      TEXT
            );
            CREATE INDEX idx_orders_source_status ON orders(source, status);
            CREATE INDEX idx_orders_order_date ON orders(order_date);
            CREATE INDEX idx_orders_source_order_number ON orders(source, order_number);
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
def apply_exclusions(tmp_path, monkeypatch):
    """Load check-orders/scripts/apply-exclusions.py with DB_PATH pointing
    at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "apply_exclusions_under_test",
        "skills/check-orders/scripts/apply-exclusions.py",
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
def render_order_alerts():
    """Load check-orders/scripts/render-order-alerts.py.

    Pure stdin→stdout renderer — no module-level state to redirect.
    Tests feed stdin via monkeypatch and read stdout via capsys.
    """
    return _load(
        "render_order_alerts_under_test",
        "skills/check-orders/scripts/render-order-alerts.py",
    )


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
    """Load check-orders/scripts/fetch-order-emails.py (#638: native Gmail
    REST via the OneCLI gateway, no Composio credentials) with DB_PATH
    redirected at a tmp_path-rooted SQLite file seeded with the orders
    fixture. Returned tuple is (module, missing_scripts_dir, db_path).

    Both heartbeat resolution paths are pointed at absent tmp dirs — this
    tile does not ship the heartbeat scripts, and the in-repo fallback
    would not exist on a runner either — so the default state is the
    fail-closed `main()` path (helper unavailable → exit 2). Tests that
    need the helpers repoint `_HEARTBEAT_MOUNT` at `tests/fakes/`, which
    carries a double for each of the four shared modules.

    The pure `fetch_order_emails(gmail, sanitize, gmail_message, queries)`
    core takes an injected `gmail` collaborator, so it needs no network
    and no real heartbeat sibling."""
    missing_scripts = tmp_path / "no-heartbeat-scripts"
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "fetch_order_emails_under_test",
        "skills/check-orders/scripts/fetch-order-emails.py",
    )
    monkeypatch.setattr(module, "_HEARTBEAT_MOUNT", str(tmp_path / "absent-mount"))
    monkeypatch.setattr(module, "_HEARTBEAT_FALLBACK", missing_scripts)
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    return module, missing_scripts, db_path


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
def list_stuck_candidates(tmp_path, monkeypatch):
    """Load check-orders/scripts/list-stuck-candidates.py with DB_PATH
    pointing at a tmp_path-rooted seeded SQLite file."""
    db_path = tmp_path / "messages.db"
    _seed_orders_db(str(db_path))
    module = _load(
        "list_stuck_candidates_under_test",
        "skills/check-orders/scripts/list-stuck-candidates.py",
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


@pytest.fixture
def heartbeat_fakes():
    """Load the `tests/fakes/` doubles for the four heartbeat modules
    fetch-order-emails.py resolves over the co-loaded `tessl__heartbeat`
    tile mount (this tile ships none of them).

    Loaded through the same file-path import the script itself uses, so
    the doubles are exercised as modules rather than as ad-hoc stubs.
    Each fake's own docstring states which parts of its real counterpart's
    behavior it mirrors and which belong to jbaruch/nanoclaw-admin's
    heartbeat suite."""
    fakes_dir = Path(__file__).resolve().parent / "fakes"
    names = {
        "sanitize_email_body": "sanitize-email-body.py",
        "google_rest": "google-rest.py",
        "gmail_ops": "gmail-ops.py",
        "gmail_message": "gmail-message.py",
    }
    return {
        name: _load(f"fake_{name}", str((fakes_dir / filename).relative_to(REPO_ROOT)))
        for name, filename in names.items()
    }
