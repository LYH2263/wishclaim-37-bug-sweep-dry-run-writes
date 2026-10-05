"""TTL expiry sweep service: dry-run vs. commit, two phases.

Dry-run only *lists* the expired locks that a commit would release. It never
touches the wishes table, never appends a ledger row, and never commits —
wall cards stay locked, detail pages still show the claim, and the ledger is
unchanged after a dry-run.

Commit is the sole path that releases expired locks. It runs inside one
transaction and either fully succeeds — every released lock is open on the
wall, gone from the original claimer's list, and present as one row in one
ledger batch — or fully rolls back. A lock that was manually released between
dry-run and commit is simply absent from the recomputed candidate set, so it
is neither released twice nor ledgered. Re-committing the same lock is a
no-op: it is already open, so it is not selected, and the ledger's
UNIQUE(wish_id, claimed_at) is a second guard.
"""
from app.engines.claim_lock import release_if_expired
from app.modules import release_ledger


def select_candidates(c, now) -> list[dict]:
    """Claimed wishes whose TTL has expired. Open / not-yet-expired /
    manually released / fulfilled rows are excluded by construction."""
    out = []
    for r in c.execute("SELECT * FROM wishes WHERE status='claimed'"):
        if release_if_expired(r["status"], r["expires_at"], now) is not None:
            out.append(
                {
                    "wish_id": r["id"],
                    "claimer": r["claimer"],
                    "claimed_at": r["claimed_at"],
                    "expires_at": r["expires_at"],
                }
            )
    return out


def dry_run(c, now) -> list[dict]:
    """Read-only preview: return the candidates without writing anything."""
    return select_candidates(c, now)


def commit(c, now) -> dict:
    """Release every still-expired lock and append the ledger batch atomically.

    Recomputes candidates at commit time, so only locks that are *actually*
    released here get ledgered — a lock manually released meanwhile is absent
    (status != 'claimed'), and a still-valid lock never expires by selection.
    If any ledger insert fails the whole transaction rolls back: the wishes
    table and the ledger can never diverge.
    """
    try:
        candidates = select_candidates(c, now)
        if not candidates:
            c.commit()
            return {"batch_id": None, "released": [], "ledgered": 0}

        batch_id = release_ledger.make_batch_id(now)
        released_at = now.isoformat()
        released = []
        for cand in candidates:
            # RETURNING reports only the row we genuinely flipped here.
            row = c.execute(
                "UPDATE wishes SET status='open', claimer=NULL, "
                "claimed_at=NULL, expires_at=NULL "
                "WHERE id=? AND status='claimed' AND expires_at=? "
                "RETURNING id, claimer, claimed_at, expires_at",
                (cand["wish_id"], cand["expires_at"]),
            ).fetchone()
            if row is not None:
                released.append(
                    {
                        "wish_id": row["id"],
                        "claimer": row["claimer"],
                        "claimed_at": row["claimed_at"],
                        "expires_at": row["expires_at"],
                    }
                )

        if not released:
            # Every dry-run candidate changed under us (manual release);
            # nothing to release or ledger.
            c.commit()
            return {"batch_id": None, "released": [], "ledgered": 0}

        # One row per actually-released lock; the UNIQUE constraint rejects a
        # double-ledger and its exception rolls the whole commit back.
        inserted = release_ledger.append(c, batch_id, released, released_at)
        if len(inserted) != len(released):
            raise RuntimeError("ledger insert mismatch: refusing partial sweep")

        c.commit()
    except Exception:
        c.rollback()
        raise

    return {
        "batch_id": batch_id,
        "released": [{**x, "released_at": released_at} for x in inserted],
        "ledgered": len(inserted),
    }
