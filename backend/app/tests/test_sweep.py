"""Service-level tests for the TTL expiry sweep, ledger and projections.

These exercise the actual sqlite-backed modules (no FastAPI needed) so they
run on the standard library alone. They pin the required semantics:

* dry-run is read-only (wish table + ledger untouched);
* commit releases and ledger rows atomically in one batch;
* re-committing the same lock never double-counts;
* manually released rows are never ledgered and never get a batch badge;
* not-yet-expired locks are never released;
* a failure mid-commit rolls BOTH the wish table and the ledger back;
* a lock re-claimed (new expires_at) between select and update is not
  clobbered by a stale candidate.
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.modules import expiry_sweep, release_ledger, sweep_projection

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
FUTURE = (NOW + timedelta(days=1)).isoformat()
PAST = (NOW - timedelta(hours=1)).isoformat()

WISH_SCHEMA = """
CREATE TABLE wishes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, note TEXT, status TEXT,
  claimer TEXT, claimed_at TEXT, expires_at TEXT, data_quality TEXT
);
"""


class SweepCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.c = sqlite3.connect(self.path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(WISH_SCHEMA)
        release_ledger.ensure_schema(self.c)
        self.c.commit()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    # ---- helpers -------------------------------------------------------
    def add_wish(self, status, claimer=None, claimed_at=None, expires_at=None):
        cur = self.c.execute(
            "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
            " VALUES (?,?,?,?,?,?,?)",
            ("t", "n", status, claimer, claimed_at, expires_at, "clean"),
        )
        self.c.commit()
        return cur.lastrowid

    def wish(self, wid):
        return dict(self.c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone())

    def ledger_count(self):
        return self.c.execute("SELECT COUNT(*) c FROM release_ledger").fetchone()["c"]

    # ---- 1. dry-run is strictly read-only ------------------------------
    def test_dry_run_is_read_only(self):
        wid = self.add_wish("claimed", "ghost", "2026-10-01T00:00:00+00:00", PAST)
        before = self.wish(wid)

        cands = expiry_sweep.dry_run(self.c, NOW)

        self.assertEqual([x["wish_id"] for x in cands], [wid])
        self.assertEqual(cands[0]["claimer"], "ghost")
        after = self.wish(wid)
        # wish table byte-for-byte unchanged
        self.assertEqual(after, before)
        self.assertEqual(after["status"], "claimed")
        self.assertEqual(after["claimer"], "ghost")
        self.assertEqual(self.ledger_count(), 0)

    def test_state_after_dry_run_wall_detail_mine(self):
        wid = self.add_wish("claimed", "ghost", "2026-10-01T00:00:00+00:00", PAST)
        expiry_sweep.dry_run(self.c, NOW)

        rows = {r["id"]: r for r in sweep_projection.wall_rows(self.c, NOW)}
        self.assertFalse(rows[wid]["claimable"], "干跑后墙卡必须仍不可领")
        self.assertIsNone(rows[wid]["released_batch_id"])
        # detail still claimed, no events
        self.assertEqual(self.wish(wid)["status"], "claimed")
        self.assertEqual(sweep_projection.wish_events(self.c, wid), [])
        # mine still shows the original claimer
        mine = [dict(r) for r in self.c.execute(
            "SELECT * FROM wishes WHERE claimer=?", ("ghost",))]
        self.assertEqual([w["id"] for w in mine], [wid])

    # ---- 2. commit flips everything together, one batch ----------------
    def test_commit_pins_one_batch_across_three_views(self):
        a = self.add_wish("claimed", "alice", "2026-10-01T00:00:00+00:00", PAST)
        b = self.add_wish("claimed", "bob", "2026-10-02T00:00:00+00:00",
                          (NOW - timedelta(minutes=5)).isoformat())

        res = expiry_sweep.commit(self.c, NOW)
        self.assertIsNotNone(res["batch_id"])
        self.assertEqual(res["ledgered"], 2)
        batch = res["batch_id"]

        # wishes opened
        for wid in (a, b):
            w = self.wish(wid)
            self.assertEqual(w["status"], "open")
            self.assertIsNone(w["claimer"])
            self.assertIsNone(w["claimed_at"])
            self.assertIsNone(w["expires_at"])

        # wall: claimable + same batch badge
        rows = {r["id"]: r for r in sweep_projection.wall_rows(self.c, NOW)}
        for wid in (a, b):
            self.assertTrue(rows[wid]["claimable"])
            self.assertEqual(rows[wid]["released_batch_id"], batch)

        # detail events carry that same batch
        for wid in (a, b):
            evs = sweep_projection.wish_events(self.c, wid)
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["batch_id"], batch)
            self.assertEqual(evs[0]["type"], "ttl_released")

        # ledger page pins the same batch with both entries
        view = sweep_projection.ledger_view(self.c)
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["batch_id"], batch)
        self.assertEqual(sorted(e["wish_id"] for e in view[0]["entries"]), [a, b])

        # "mine" for the original claimers is empty right after commit
        for claimer in ("alice", "bob"):
            mine = self.c.execute(
                "SELECT * FROM wishes WHERE claimer=?", (claimer,)).fetchall()
            self.assertEqual(list(mine), [], "提交后原人的「我的认领」必须立即释放")

    # ---- 3. same lock committed twice never double-counts --------------
    def test_commit_is_idempotent(self):
        wid = self.add_wish("claimed", "ghost", "2026-10-01T00:00:00+00:00", PAST)
        first = expiry_sweep.commit(self.c, NOW)
        self.assertEqual(first["ledgered"], 1)

        second = expiry_sweep.commit(self.c, NOW)
        self.assertIsNone(second["batch_id"])
        self.assertEqual(second["ledgered"], 0)
        self.assertEqual(self.ledger_count(), 1)
        # still just one detail event / one ledger batch
        self.assertEqual(len(sweep_projection.wish_events(self.c, wid)), 1)
        self.assertEqual(len(sweep_projection.ledger_view(self.c)), 1)

    # ---- 4. not-yet-expired locks are never released -------------------
    def test_unexpired_lock_survives_dry_run_and_commit(self):
        wid = self.add_wish("claimed", "carol", "2026-10-04T00:00:00+00:00", FUTURE)
        self.assertEqual(expiry_sweep.dry_run(self.c, NOW), [])
        res = expiry_sweep.commit(self.c, NOW)
        self.assertEqual(res["ledgered"], 0)
        w = self.wish(wid)
        self.assertEqual(w["status"], "claimed")
        self.assertEqual(w["claimer"], "carol")
        self.assertEqual(self.ledger_count(), 0)
        rows = {r["id"]: r for r in sweep_projection.wall_rows(self.c, NOW)}
        self.assertFalse(rows[wid]["claimable"])

    # ---- 5. manual release: never ledgered, no badge, no event ---------
    def test_manual_release_never_ledgered(self):
        wid = self.add_wish("claimed", "dave", "2026-10-01T00:00:00+00:00", PAST)
        # simulate the /release endpoint
        self.c.execute(
            "UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL,"
            " expires_at=NULL WHERE id=?", (wid,))
        self.c.commit()

        self.assertEqual(expiry_sweep.dry_run(self.c, NOW), [])
        res = expiry_sweep.commit(self.c, NOW)
        self.assertEqual(res["ledgered"], 0)
        self.assertEqual(self.ledger_count(), 0)

        rows = {r["id"]: r for r in sweep_projection.wall_rows(self.c, NOW)}
        self.assertTrue(rows[wid]["claimable"])  # released can be re-claimed
        self.assertIsNone(rows[wid]["released_batch_id"])  # no batch badge
        self.assertEqual(sweep_projection.wish_events(self.c, wid), [])
        self.assertEqual(sweep_projection.ledger_view(self.c), [])

    # ---- 6. dry-run + manual release interleave: no double, no miss ----
    def test_dry_run_then_manual_release_then_commit(self):
        a = self.add_wish("claimed", "alice", "2026-10-01T00:00:00+00:00", PAST)
        b = self.add_wish("claimed", "bob", "2026-10-02T00:00:00+00:00", PAST)
        # dry-run saw both ...
        preview = expiry_sweep.dry_run(self.c, NOW)
        self.assertEqual(sorted(x["wish_id"] for x in preview), [a, b])
        # ... then A is manually released before commit
        self.c.execute("UPDATE wishes SET status='released', claimer=NULL,"
                       " claimed_at=NULL, expires_at=NULL WHERE id=?", (a,))
        self.c.commit()

        res = expiry_sweep.commit(self.c, NOW)
        self.assertEqual(res["ledgered"], 1)  # only B
        ledgered_ids = [r["wish_id"] for r in res["released"]]
        self.assertEqual(ledgered_ids, [b])  # A not wrongly ledgered

        view = sweep_projection.ledger_view(self.c)
        self.assertEqual([e["wish_id"] for e in view[0]["entries"]], [b])
        rows = {r["id"]: r for r in sweep_projection.wall_rows(self.c, NOW)}
        self.assertIsNone(rows[a]["released_batch_id"])  # manual side
        self.assertEqual(rows[b]["released_batch_id"], res["batch_id"])  # sweep side
        self.assertTrue(rows[a]["claimable"] and rows[b]["claimable"])

    # ---- 7. failure mid-commit rolls BOTH sides back -------------------
    def test_commit_failure_rolls_back_wishes_and_ledger(self):
        a = self.add_wish("claimed", "alice", "2026-10-01T00:00:00+00:00", PAST)
        b = self.add_wish("claimed", "bob", "2026-10-02T00:00:00+00:00", PAST)

        orig_append = release_ledger.append

        def boom(c, batch_id, candidates, released_at):
            raise RuntimeError("ledger disk full")

        release_ledger.append = boom
        try:
            with self.assertRaises(RuntimeError):
                expiry_sweep.commit(self.c, NOW)
        finally:
            release_ledger.append = orig_append

        # nothing half-applied: both wishes back to claimed, ledger empty
        for wid, claimer in ((a, "alice"), (b, "bob")):
            w = self.wish(wid)
            self.assertEqual(w["status"], "claimed", "失败必须回滚愿望表")
            self.assertEqual(w["claimer"], claimer)
        self.assertEqual(self.ledger_count(), 0, "失败必须回滚台账")
        # a later healthy commit still works on the rolled-back state
        ok = expiry_sweep.commit(self.c, NOW)
        self.assertEqual(ok["ledgered"], 2)

    # ---- 8. stale candidate (lock re-claimed) is not clobbered ---------
    def test_conditional_update_skips_reclaimed_lock(self):
        wid = self.add_wish("claimed", "zoe", "2026-10-01T00:00:00+00:00", FUTURE)

        # Pretend a concurrent re-claim changed expires_at AFTER selection:
        # feed commit a stale candidate carrying the old expires_at.
        stale = [{"wish_id": wid, "claimer": "zoe",
                  "claimed_at": "2026-10-01T00:00:00+00:00", "expires_at": PAST}]
        orig_select = expiry_sweep.select_candidates
        expiry_sweep.select_candidates = lambda c, now: list(stale)
        try:
            res = expiry_sweep.commit(self.c, NOW)
        finally:
            expiry_sweep.select_candidates = orig_select

        self.assertEqual(res["ledgered"], 0)
        w = self.wish(wid)  # the live (future) lock is untouched
        self.assertEqual(w["status"], "claimed")
        self.assertEqual(w["claimer"], "zoe")
        self.assertEqual(w["expires_at"], FUTURE)
        self.assertEqual(self.ledger_count(), 0)


if __name__ == "__main__":
    unittest.main()
