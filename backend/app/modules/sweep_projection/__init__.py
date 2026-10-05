"""Read projections pinning a sweep commit across wall cards, detail events
and the ledger page. Every released row carries the same batch_id found in
the ledger, so the three views tell one story per commit."""
from app.engines.claim_lock import claim_allowed
from app.modules import release_ledger


def wall_rows(c, now) -> list[dict]:
    """Wishes for the wall with a `claimable` flag and, where a TTL sweep
    released a prior lock, the batch that did it (`released_batch_id`)."""
    last_batch_by_wish = {}
    for row in release_ledger.list_rows(c):
        last_batch_by_wish.setdefault(row["wish_id"], row["batch_id"])

    rows = []
    for r in c.execute("SELECT * FROM wishes ORDER BY id DESC"):
        d = dict(r)
        d["claimable"] = claim_allowed(d["status"], d["claimer"], now, d["expires_at"])["ok"]
        # Same single source of truth as the ledger page and the detail
        # events: a card shows the batch ONLY if the ledger has that release.
        # Manually released rows are absent from the ledger -> None here.
        d["released_batch_id"] = last_batch_by_wish.get(d["id"])
        rows.append(d)
    return rows


def wish_events(c, wish_id: int) -> list[dict]:
    """Detail-page event stream: every TTL release for this wish, newest
    first, each tagged with its commit batch_id."""
    events = [
        {
            "type": "ttl_released",
            "batch_id": row["batch_id"],
            "claimer": row["claimer"],
            "claimed_at": row["claimed_at"],
            "at": row["released_at"],
        }
        for row in reversed(release_ledger.for_wish(c, wish_id))
    ]
    return events


def ledger_view(c) -> list[dict]:
    """Ledger page: rows grouped under their commit batch, newest batch first."""
    batches: dict[str, dict] = {}
    order: list[str] = []
    for row in release_ledger.list_rows(c):
        b = row["batch_id"]
        if b not in batches:
            batches[b] = {"batch_id": b, "released_at": row["released_at"], "entries": []}
            order.append(b)
        batches[b]["entries"].append(
            {
                "wish_id": row["wish_id"],
                "claimer": row["claimer"],
                "claimed_at": row["claimed_at"],
            }
        )
    return [batches[b] for b in order]
