"""TTL expiry sweep service: dry-run vs. commit, two phases.

Dry-run is strictly read-only: it lists the expired locks that a commit
*would* release (id + original claimer) and never touches the wish table,
never writes the ledger, and never commits.

Commit runs the release and the ledger append inside ONE transaction so the
wish table and the ledger move together — they can never be observed
half-applied:

* candidates are recomputed at commit time (rows manually released in
  between are already gone from the ``status='claimed'`` set);
* each candidate is released with a *conditional* UPDATE that additionally
  matches ``expires_at``, so it only fires when the exact expired lock seen
  during selection is still held — a concurrently re-claimed lock (new
  ``expires_at``) is never clobbered;
* one ledger row is appended ONLY for rows whose conditional UPDATE really
  fired, all sharing one ``batch_id``;
* everything commits together; any failure rolls both sides back.

A lock manually released (``status='released'``) is never a candidate and is
therefore never ledgered. Re-committing the same lock finds it already open
and inserts nothing — the ledger ``UNIQUE(wish_id, claimed_at)`` is a second
guard against double counting.
"""
from app.engines.claim_lock import release_if_expired
from app.modules import release_ledger


def select_candidates(c, now) -> list[dict]:
    """Claimed wishes whose TTL has expired. Open / not-yet-expired /
    manually released rows are excluded by construction (manual release
    sets status='released', which this never matches)."""
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
    """Read-only preview: the expired locks a commit would release.

    Performs no UPDATE/INSERT and issues no commit/rollback, so the wish
    table and ledger are byte-for-byte unchanged afterwards.
    """
    return select_candidates(c, now)


def commit(c, now) -> dict:
    """Release every still-expired lock and append one ledger batch,
    atomically.

    Returns ``batch_id`` plus exactly the rows that were both released and
    ledgered in this batch. Re-running commit for the same locks releases
    nothing and ledger-adds nothing.
    """
    candidates = select_candidates(c, now)
    if not candidates:
        return {"batch_id": None, "released": [], "ledgered": 0}

    released_at = now.isoformat()
    released: list[dict] = []
    try:
        for cand in candidates:
            # Conditional on status AND the exact expires_at seen during
            # selection: fires only while that exact expired lock is held.
            cur = c.execute(
                "UPDATE wishes SET status='open', claimer=NULL, "
                "claimed_at=NULL, expires_at=NULL "
                "WHERE id=? AND status='claimed' AND expires_at=?",
                (cand["wish_id"], cand["expires_at"]),
            )
            if cur.rowcount == 1:
                released.append({**cand, "released_at": released_at})

        # Every candidate vanished between select and update (e.g. a
        # concurrent re-claim changed expires_at): nothing to ledger, no
        # phantom batch. The UPDATEs above matched zero rows.
        if not released:
            c.commit()
            return {"batch_id": None, "released": [], "ledgered": 0}

        # Ledger ONLY the rows this commit really released, all under one
        # batch, in the SAME transaction as the UPDATEs above.
        batch_id = release_ledger.make_batch_id(now)
        inserted = release_ledger.append(c, batch_id, released, released_at)
        c.commit()
    except Exception:
        c.rollback()  # wishes and ledger snap back to the pre-commit state
        raise

    return {
        "batch_id": batch_id,
        "released": inserted,
        "ledgered": len(inserted),
    }
