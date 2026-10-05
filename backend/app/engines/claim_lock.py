"""Claim mutex + TTL release for wishes."""
from datetime import datetime, timedelta, timezone

def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def claim_allowed(status: str, claimer: str | None, now: datetime, expires_at: str | None) -> dict:
    """Only genuinely open wishes can be claimed.

    A claimed lock stays locked — even past its TTL — until the expiry sweep
    is *committed*. The sweep commit is the sole path that releases an expired
    lock (and ledgers it); callers may not reclaim an expired lock directly,
    which would bypass the ledger and leave the wall / mine views disagreeing.
    """
    if status == "fulfilled":
        return {"ok": False, "reason": "already_fulfilled"}
    if status == "claimed":
        return {"ok": False, "reason": "locked"}
    if status in ("open", "released"):
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": "bad_status"}

def lock_payload(claimer: str, now: datetime, ttl_seconds: int) -> dict:
    exp = now + timedelta(seconds=ttl_seconds)
    return {
        "status": "claimed",
        "claimer": claimer,
        "claimed_at": now.isoformat(),
        "expires_at": exp.isoformat(),
    }

def release_if_expired(status: str, expires_at: str | None, now: datetime) -> dict | None:
    if status != "claimed" or not expires_at:
        return None
    if parse_ts(expires_at) <= now:
        return {"status": "open", "claimer": None, "claimed_at": None, "expires_at": None}
    return None
