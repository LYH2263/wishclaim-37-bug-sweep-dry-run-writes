"""Append-only ledger recording TTL-expiry sweeps.

One row per actually released expired lock. The UNIQUE(wish_id, claimed_at)
key makes committing the *same* expired lock twice idempotent: a re-commit
finds the lock already open and selects nothing; even if it did not, the
insert would be ignored. A later re-claim gets a fresh claimed_at and is a
new ledger row.
"""
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS release_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  wish_id INTEGER NOT NULL,
  claimer TEXT,
  claimed_at TEXT,
  released_at TEXT NOT NULL,
  UNIQUE(wish_id, claimed_at)
);
"""


def ensure_schema(c):
    c.executescript(SCHEMA)


def append(c, batch_id: str, candidates: list[dict], released_at: str) -> list[dict]:
    """Append ledger rows for one committed batch; skip already-ledgered locks.

    Returns the rows actually inserted (dicts), in candidate order.
    """
    inserted = []
    for cand in candidates:
        cur = c.execute(
            "INSERT OR IGNORE INTO release_ledger"
            "(batch_id,wish_id,claimer,claimed_at,released_at) VALUES (?,?,?,?,?)",
            (batch_id, cand["wish_id"], cand["claimer"], cand["claimed_at"], released_at),
        )
        if cur.rowcount == 1:
            inserted.append(
                {
                    "batch_id": batch_id,
                    "wish_id": cand["wish_id"],
                    "claimer": cand["claimer"],
                    "claimed_at": cand["claimed_at"],
                    "released_at": released_at,
                }
            )
    return inserted


def list_rows(c, batch_id: str | None = None) -> list[dict]:
    if batch_id is not None:
        rows = c.execute(
            "SELECT * FROM release_ledger WHERE batch_id=? ORDER BY id", (batch_id,)
        )
    else:
        rows = c.execute("SELECT * FROM release_ledger ORDER BY id DESC")
    return [dict(r) for r in rows]


def latest_batch_id(c) -> str | None:
    r = c.execute("SELECT batch_id FROM release_ledger ORDER BY id DESC LIMIT 1").fetchone()
    return r["batch_id"] if r else None


def for_wish(c, wish_id: int) -> list[dict]:
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM release_ledger WHERE wish_id=? ORDER BY id", (wish_id,)
        )
    ]


def make_batch_id(now: datetime) -> str:
    return "sweep-" + now.strftime("%Y%m%dT%H%M%S%fZ")
