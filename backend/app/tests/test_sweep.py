"""Regression tests for the TTL expiry sweep two-phase contract.

Covers: dry-run is strictly read-only; only commit releases and ledgers;
commit is atomic; no double-ledger; manually released rows never enter the
ledger; unexpired locks are never touched; and wall / detail / ledger / mine
views agree on the same batch.
"""
import sqlite3

import pytest

from app.engines.claim_lock import claim_allowed
from app.modules import expiry_sweep, release_ledger, sweep_projection

WISHES_DDL = """
CREATE TABLE wishes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, note TEXT, status TEXT,
  claimer TEXT, claimed_at TEXT, expires_at TEXT, data_quality TEXT
);
"""

T0 = "2026-10-01T00:00:00+00:00"
PAST = "2026-10-01T00:01:00+00:00"   # an expired lock, evaluated at NOW
FUTURE = "2026-10-03T00:00:00+00:00"  # still valid at NOW
NOW = "2026-10-02T00:00:00+00:00"

from datetime import datetime, timezone


def now_dt():
    return datetime.fromisoformat(NOW)


@pytest.fixture()
def c():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(WISHES_DDL)
    release_ledger.ensure_schema(conn)
    yield conn
    conn.close()


def add_wish(conn, status="claimed", claimer="alice", expires_at=PAST,
             claimed_at=T0):
    cur = conn.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,"
        "data_quality) VALUES (?,?,?,?,?,?,?)",
        ("w", "", status, claimer, claimed_at, expires_at, "clean"),
    )
    conn.commit()
    return cur.lastrowid


def by_id(conn, wid):
    return dict(conn.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone())


# ---- dry run -------------------------------------------------------------

def test_dry_run_lists_only_expired_and_writes_nothing(c):
    expired = add_wish(c, expires_at=PAST)
    fresh = add_wish(c, claimer="bob", expires_at=FUTURE)
    open_row = add_wish(c, status="open", claimer=None,
                        claimed_at=None, expires_at=None)

    cands = expiry_sweep.dry_run(c, now_dt())
    assert [x["wish_id"] for x in cands] == [expired]
    assert cands[0]["claimer"] == "alice"

    # Nothing changed: expired lock still claimed, ledger still empty.
    assert by_id(c, expired)["status"] == "claimed"
    assert by_id(c, expired)["claimer"] == "alice"
    assert by_id(c, fresh)["claimer"] == "bob"
    assert release_ledger.list_rows(c) == []


def test_expired_lock_not_claimable_before_commit(c):
    wid = add_wish(c, expires_at=PAST)
    allowed = claim_allowed("claimed", "alice", now_dt(), PAST)
    assert allowed["ok"] is False
    wall = {w["id"]: w for w in sweep_projection.wall_rows(c, now_dt())}
    assert wall[wid]["claimable"] is False
    assert wall[wid]["released_batch_id"] is None


# ---- commit --------------------------------------------------------------

def test_commit_releases_and_pins_one_batch_across_views(c):
    w1 = add_wish(c, claimer="alice")
    w2 = add_wish(c, claimer="bob")

    res = expiry_sweep.commit(c, now_dt())
    batch = res["batch_id"]
    assert batch is not None
    assert res["ledgered"] == 2
    assert {x["wish_id"] for x in res["released"]} == {w1, w2}

    for wid in (w1, w2):
        d = by_id(c, wid)
        assert d["status"] == "open"
        assert d["claimer"] is None
        assert d["claimed_at"] is None
        assert d["expires_at"] is None

    # Wall: open, claimable, tagged with the same batch.
    wall = {w["id"]: w for w in sweep_projection.wall_rows(c, now_dt())}
    for wid in (w1, w2):
        assert wall[wid]["claimable"] is True
        assert wall[wid]["released_batch_id"] == batch

    # Detail events carry the batch.
    for wid in (w1, w2):
        evs = sweep_projection.wish_events(c, wid)
        assert len(evs) == 1
        assert evs[0]["type"] == "ttl_released"
        assert evs[0]["batch_id"] == batch

    # Ledger page pins the same batch, two entries.
    view = sweep_projection.ledger_view(c)
    assert len(view) == 1
    assert view[0]["batch_id"] == batch
    assert {e["wish_id"] for e in view[0]["entries"]} == {w1, w2}

    # Gone from the original claimers' lists.
    mine = [dict(r) for r in c.execute(
        "SELECT * FROM wishes WHERE claimer='alice'")]
    assert mine == []


def test_commit_twice_does_not_double_ledger(c):
    wid = add_wish(c)
    first = expiry_sweep.commit(c, now_dt())
    assert first["ledgered"] == 1

    second = expiry_sweep.commit(c, now_dt())
    assert second["batch_id"] is None
    assert second["ledgered"] == 0

    rows = release_ledger.list_rows(c)
    assert len(rows) == 1
    assert rows[0]["batch_id"] == first["batch_id"]
    events = sweep_projection.wish_events(c, wid)
    assert len(events) == 1


def test_unexpired_lock_not_released_by_commit(c):
    fresh = add_wish(c, claimer="bob", expires_at=FUTURE)
    res = expiry_sweep.commit(c, now_dt())
    assert res["ledgered"] == 0
    d = by_id(c, fresh)
    assert d["status"] == "claimed"
    assert d["claimer"] == "bob"
    assert release_ledger.list_rows(c) == []


# ---- manual release interplay -------------------------------------------

def test_manual_release_is_never_ledgered(c):
    wid = add_wish(c)
    c.execute(
        "UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, "
        "expires_at=NULL WHERE id=?", (wid,))
    c.commit()

    res = expiry_sweep.commit(c, now_dt())
    assert res["batch_id"] is None
    assert res["ledgered"] == 0
    assert release_ledger.list_rows(c) == []
    wall = {w["id"]: w for w in sweep_projection.wall_rows(c, now_dt())}
    assert wall[wid]["released_batch_id"] is None
    assert sweep_projection.wish_events(c, wid) == []


def test_dry_run_then_manual_release_then_commit(c):
    kept = add_wish(c, claimer="alice")
    stolen = add_wish(c, claimer="carol")

    # Dry-run sees both, changes nothing.
    cands = expiry_sweep.dry_run(c, now_dt())
    assert {x["wish_id"] for x in cands} == {kept, stolen}

    # Carol's lock is manually released before commit.
    c.execute(
        "UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, "
        "expires_at=NULL WHERE id=?", (stolen,))
    c.commit()

    res = expiry_sweep.commit(c, now_dt())
    assert res["ledgered"] == 1
    assert [x["wish_id"] for x in res["released"]] == [kept]

    # The swept lock is ledgered; the manually released one is not.
    rows = release_ledger.list_rows(c)
    assert [r["wish_id"] for r in rows] == [kept]
    assert by_id(c, kept)["status"] == "open"
    assert by_id(c, stolen)["status"] == "released"
    assert sweep_projection.wish_events(c, stolen) == []


# ---- atomicity -----------------------------------------------------------

def test_commit_rolls_back_when_ledger_write_fails(c, monkeypatch):
    wid = add_wish(c)

    def boom(*a, **k):
        raise RuntimeError("ledger disk full")

    monkeypatch.setattr(release_ledger, "append", boom)
    with pytest.raises(RuntimeError):
        expiry_sweep.commit(c, now_dt())

    # Wishes table rolled back: lock still held, no half-open row.
    d = by_id(c, wid)
    assert d["status"] == "claimed"
    assert d["claimer"] == "alice"
    assert d["expires_at"] == PAST
    assert release_ledger.list_rows(c) == []


def test_commit_rolls_back_on_partial_double_ledger(c, monkeypatch):
    add_wish(c, claimer="alice")
    add_wish(c, claimer="bob")

    # Simulate a UNIQUE-constraint collision dropping one insert.
    real_append = release_ledger.append

    def partial(conn, batch_id, candidates, released_at):
        rows = real_append(conn, batch_id, candidates[:1], released_at)
        return rows  # fewer rows than released -> mismatch -> rollback

    monkeypatch.setattr(release_ledger, "append", partial)
    with pytest.raises(RuntimeError):
        expiry_sweep.commit(c, now_dt())

    assert release_ledger.list_rows(c) == []
    for r in c.execute("SELECT * FROM wishes"):
        assert r["status"] == "claimed"
        assert r["claimer"] in ("alice", "bob")
