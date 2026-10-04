"""TTL expiry sweep service: dry-run vs. commit, two phases.

Dry-run lists the expired locks that *would* be released and never writes.
Commit recomputes the same candidate set at commit time (so a lock manually
released in between is simply absent — never ledgered), releases each still
matching lock, and appends one ledger row per released lock.

Candidate selection has a single entry point, :func:`select_candidates`,
shared by both phases, so their sets are identical barring concurrent
manual release or time actually advancing across an expiry boundary.
"""
from app.engines.claim_lock import release_if_expired
from app.modules import release_ledger


def select_candidates(c, now) -> list[dict]:
    """Claimed wishes whose TTL has expired. Open / not-yet-expired /
    manually released rows are excluded by construction."""
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
    cands = select_candidates(c, now)
    for cand in cands:
        c.execute(
            "UPDATE wishes SET status='open', claimer=NULL WHERE id=? AND status='claimed'",
            (cand["wish_id"],),
        )
    c.commit()
    return cands


def commit(c, now) -> dict:
    """Release every still-expired lock and append the ledger batch.

    Re-running commit for the same expired locks ledger-adds nothing:
    they are already open, so candidates come back empty; the ledger's
    UNIQUE(wish_id, claimed_at) is a second guard.
    """
    candidates = select_candidates(c, now)
    if not candidates:
        return {"batch_id": None, "released": [], "ledgered": 0}

    batch_id = release_ledger.make_batch_id(now)
    released_at = now.isoformat()
    still_locked = []
    for cand in candidates:
        cur = c.execute(
            "UPDATE wishes SET status='open', claimer=NULL, claimed_at=NULL, expires_at=NULL "
            "WHERE id=? AND status='claimed' AND expires_at=? ",
            (cand["wish_id"], cand["expires_at"]),
        )
        if cur.rowcount == 1:
            still_locked.append(cand)

    extra = select_candidates(c, now) or still_locked
    inserted = release_ledger.append(c, batch_id, extra or still_locked, released_at)
    c.commit()
    return {
        "batch_id": batch_id,
        "released": [{**x, "released_at": released_at} for x in inserted],
        "ledgered": len(inserted),
    }
