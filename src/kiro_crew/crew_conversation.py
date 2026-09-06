"""Crew conversation index — the thin per-(human × member) conversation entity.

A crew member's DM thread on the Crew Members page is a *conversation* between
one human and one member. Its lifetime is longer than any single session: the
DM slot can be rebuilt, rotated, or re-bound, and a worker session the member
dispatched may hand a result back into it. The conversation therefore needs an
identity of its own — but it must NOT become a second transcript.

This module keeps that identity **thin**:

* it stores **pointers**, never bodies — an entry is either a
  ``(session_key, mid)`` reference into a session's JSONL transcript, or a
  native *escalation* record whose text still lives on the transcript row;
* the human-facing projection (what the chat view shows) is computed from the
  referenced transcripts, so a conversation can never disagree with the
  sessions it points at;
* ``needs_you`` is **derived** from the pending escalations on the index, not
  stored on the slot — the slot is a process, the conversation is the thing the
  human is in.

The key is ``dm:<slug>`` today (one human, one member). The record already
carries a ``participants`` list and a ``sessions`` list rather than a single
member/session field, so a later ``goal:<id>`` conversation (one goal, several
members plus the human) is a new key shape, not a schema migration.

Same placement discipline as the activity log (:mod:`kiro_crew.members`):
the file sits in the member's own directory, beside ``activity.jsonl``, and is
NOT the trust binding — the binding is the identity authority and stays
strict-shape; this is mutable UI state and stays out of the keystone-gated
subtree.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.members import member_dir, validate_slug

logger = logging.getLogger(__name__)

#: File name inside ``member_dir(slug)``.
CONVERSATION_FILE_NAME = "conversation.json"

SCHEMA_VERSION = 1

#: Cap on SETTLED entries per conversation — pointers are ~200 bytes, so the
#: settled history stays around 100 KiB. Eviction is oldest-first over settled
#: entries only: a pending escalation is never dropped by the cap (a badge that
#: vanished without an answer, a deadline or a default would be a lost
#: decision, not a trimmed log), so a record with more than this many OPEN
#: decisions is allowed to exceed the cap.
_MAX_ENTRIES = 500

#: Hard ceiling on OPEN decisions per member. Pending records are never evicted
#: by the cap above, so without this a member escalating deadline-free in a
#: loop, with nobody answering, would grow the file without bound — every
#: write rewrites it whole. Past the ceiling ``record_escalation`` REFUSES
#: (``EscalationBacklogFull``), atomically under the slug lock, and the caller
#: surfaces that to the member: the fix for a hundred unanswered decisions is
#: an answer or a stop, not a hundred-and-first card. Generous enough that a
#: human's backlog never hits it; a runaway loop hits it in seconds.
MAX_PENDING_ESCALATIONS = 50


class EscalationBacklogFull(RuntimeError):
    """Raised by :func:`record_escalation` when the member already has
    :data:`MAX_PENDING_ESCALATIONS` open decisions."""

    def __init__(self, slug: str, pending: int) -> None:
        super().__init__(
            f"{slug} already has {pending} unanswered escalations "
            f"(limit {MAX_PENDING_ESCALATIONS})"
        )
        self.slug = slug
        self.pending = pending


#: Cap on option labels an escalation may offer (mirrors ``ask_question``).
MAX_ESCALATION_OPTIONS = 6

#: Transcript role of an escalation card row. Spelled here as well as in
#: ``session_control.ESCALATION_ROLE`` (which imports this module, so the
#: dependency cannot run the other way); ``test_escalation`` pins the two equal.
ESCALATION_ROW_ROLE = "escalation"

# One lock per slug around every read-modify-write. All writers live in one
# gateway process (the dashboard's own executor threads: the escalation path,
# the reply hook), but they run on different threads, so
# without this two concurrent mutations would each load the file, each append
# their entry and the second `atomic_write` would silently drop the first.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# In-memory pending view for the hot read path (`needs_you` runs inside the
# slot projection, ON the event loop, on every sidebar push): index file path
# -> (id, deadline) of the records stored as pending. Never read from disk on
# the read path; the writers refresh it after every write and `prime` loads it
# once per member.
_PENDING_CACHE: dict[str, list[tuple[str, str | None]]] = {}

# slug -> (the raw ``KIROCREW_HOME`` value it was resolved under, its resolved
# index-file cache key). The read path (`pending_ids`, `is_primed`) runs ON the
# event loop and must never resolve a filesystem path there (`member_dir`
# calls `.resolve()`, which stats/readlinks and can freeze the loop on a
# stalled filesystem). Only the OFF-LOOP writers and `prime` compute the key
# (`_resolve_cache_key`), recording it here; the on-loop reader looks it up by
# slug with no IO. A data-home change is handled without IO too: the raw env
# value is memoized with the key, and a lookup under a different value is
# treated as unresolved — so the reader reads "nothing pending" until the next
# off-loop writer/prime recomputes the key, never another home's view.
_CACHE_KEY_BY_SLUG: dict[str, tuple[str | None, str]] = {}


def _raw_home() -> str | None:
    """The raw ``KIROCREW_HOME`` value — an environment read, no filesystem."""
    return os.environ.get("KIROCREW_HOME")


def _resolve_cache_key(slug: str) -> str:
    """Resolve *slug*'s index-file cache key AND memoize it by slug. Touches
    the filesystem (`.resolve()` via `conversation_path`), so call it only
    OFF the event loop — the writers and `prime` already run off-loop."""
    key = str(conversation_path(slug))
    _CACHE_KEY_BY_SLUG[slug] = (_raw_home(), key)
    return key


def _memo_cache_key(slug: str) -> str | None:
    """The slug's cache key from memory, or ``None`` if no off-loop path has
    resolved it yet — or resolved it under a different ``KIROCREW_HOME``, in
    which case the mapping is stale and must not project that home's view.
    No filesystem access — safe on the event loop."""
    memo = _CACHE_KEY_BY_SLUG.get(slug)
    if memo is None or memo[0] != _raw_home():
        return None
    return memo[1]


def _lock_for(slug: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(slug)
        if lock is None:
            lock = _LOCKS[slug] = threading.Lock()
        return lock


def conversation_id(slug: str) -> str:
    """The conversation key for a member's 1:1 DM with the human."""
    return f"dm:{validate_slug(slug)}"


def conversation_path(slug: str) -> Path:
    """Absolute path of one member's conversation index (not created)."""
    return member_dir(slug) / CONVERSATION_FILE_NAME


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` or offset) to an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: Bounds on a relative deadline: below a minute the veto window is not real;
#: above a week the escalation should have been a decision, not a window.
MIN_DEADLINE_SECS = 60
MAX_DEADLINE_SECS = 7 * 86400


def resolve_deadline(value: Any, *, now: datetime | None = None) -> str | None:
    """Normalise a caller-supplied deadline to an absolute ISO timestamp.

    Accepts an ISO-8601 timestamp, a bare number of seconds, or a duration
    such as ``30m`` / ``2h`` / ``900s`` / ``1d``. Returns ``None`` for an
    empty value. Raises :class:`ValueError` for anything unparseable or a
    window outside ``[MIN_DEADLINE_SECS, MAX_DEADLINE_SECS]`` from *now*.
    """
    if value is None:
        return None
    base = now or datetime.now(timezone.utc)
    if isinstance(value, bool):
        raise ValueError("deadline must be a timestamp or a duration")
    if isinstance(value, (int, float)):
        secs = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        absolute = parse_ts(text)
        if absolute is not None:
            secs = (absolute - base).total_seconds()
        else:
            unit = text[-1].lower()
            number = text[:-1].strip()
            if unit in _DURATION_UNITS and number.replace(".", "", 1).isdigit():
                secs = float(number) * _DURATION_UNITS[unit]
            elif text.replace(".", "", 1).isdigit():
                secs = float(text)
            else:
                raise ValueError("deadline must be ISO-8601 or a duration like 30m / 2h / 900s")
    if secs < MIN_DEADLINE_SECS or secs > MAX_DEADLINE_SECS:
        raise ValueError(
            f"deadline must be between {MIN_DEADLINE_SECS}s and {MAX_DEADLINE_SECS // 86400}d from now"
        )
    return _now_iso(base + timedelta(seconds=secs))


#: The one spelling of an escalation id. Boundaries that accept an id from a
#: client (the chat send handler's ``meta.escalation_id``) validate against this
#: rather than trusting free text into a queue entry.
ESCALATION_ID_RE = re.compile(r"^esc-[0-9a-f]{16}$")


def new_escalation_id() -> str:
    """Mint one escalation id (``esc-<16 hex>``); random for the same reason
    ``history.mint_row_mid`` is — a counter rebased after restore can collide."""
    return f"esc-{uuid.uuid4().hex[:16]}"


def _scaffold(slug: str) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "conversation_id": conversation_id(slug),
        "participants": [],
        "sessions": [],
        "entries": [],
    }


def _parse_file(path: Path, slug: str) -> dict[str, Any] | None:
    """Parse the index file, or ``None`` when missing/unreadable/misshapen."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValueError:
        logger.warning("conversation index unreadable for %s", slug, exc_info=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    record = _scaffold(slug)
    # Type-check every field a writer later mutates: a hand-edited or torn file
    # with ``"participants": null`` must read as "no participants", not crash the
    # next ``record_escalation`` in ``_ensure_participants``.
    if isinstance(data.get("conversation_id"), str) and data["conversation_id"]:
        record["conversation_id"] = data["conversation_id"]
    if isinstance(data.get("version"), int):
        record["version"] = data["version"]
    record["participants"] = (
        [p for p in (data.get("participants") or []) if isinstance(p, dict)]
        if isinstance(data.get("participants"), list)
        else []
    )
    record["sessions"] = (
        [s for s in (data.get("sessions") or []) if isinstance(s, str)]
        if isinstance(data.get("sessions"), list)
        else []
    )
    record["entries"] = [e for e in data["entries"] if isinstance(e, dict)]
    return record


class IndexUnreadable(RuntimeError):
    """The index file EXISTS but cannot be read as an index (torn write, hand
    edit, wrong shape). Writers refuse rather than overwrite it — the transcript
    still holds every card, and the next roster read rebuilds the index from it
    (:func:`reconcile_with_transcript`); overwriting would destroy what the
    rebuild needs to distinguish."""


def _load_index(slug: str) -> tuple[dict[str, Any], str]:
    """``(record, state)`` where state is ``"missing"`` (no file: the scaffold
    IS the truth), ``"ok"`` (parsed), or ``"unreadable"`` (a file is there but
    not an index; the scaffold stands in for reading only)."""
    path = conversation_path(slug)
    if not path.exists():
        return _scaffold(slug), "missing"
    parsed = _parse_file(path, slug)
    if parsed is None:
        return _scaffold(slug), "unreadable"
    return parsed, "ok"


def read_conversation(slug: str) -> dict[str, Any]:
    """Load a member's conversation index; a missing or unreadable file reads
    as an empty scaffold (never raises — the index is derived state).

    Always parses: callers mutate what they get back, so no shared cached
    record is ever handed out. The hot path (:func:`needs_you`) has its own
    scalar cache.
    """
    return _load_index(slug)[0]


def index_unreadable(slug: str) -> bool:
    """Whether an index file exists for *slug* but cannot be read. The roster
    uses this to force a reconciliation regardless of the transcript's
    generation: a corrupted index is a change the transcript's mtime does not
    record."""
    return _load_index(slug)[1] == "unreadable"


def _load_for_write(slug: str) -> dict[str, Any]:
    """The record a WRITER may mutate. Caller holds the slug lock. Refuses an
    unreadable file (see :class:`IndexUnreadable`): a writer that started from
    the scaffold would overwrite the only copy of a possibly-recoverable
    lifecycle; a missing file is simply the first write."""
    record, state = _load_index(slug)
    if state == "unreadable":
        raise IndexUnreadable(f"conversation index for {slug} exists but cannot be read")
    return record


def _settled(entry: dict[str, Any]) -> bool:
    return not (entry.get("type") == "escalation" and entry.get("state") == "pending")


def _write_conversation(slug: str, record: dict[str, Any]) -> None:
    """Persist *record*. Caller holds the slug lock."""
    path = conversation_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = record["entries"]
    overflow = len(entries) - _MAX_ENTRIES
    if overflow > 0:
        # Evict the oldest SETTLED entries only. A pending escalation is never
        # evicted: if every entry is an open decision the file simply exceeds
        # the cap, because a lost decision is worse than a large file (500
        # unanswered escalations is a member that needs stopping, not trimming).
        keep: list[dict[str, Any]] = []
        for entry in entries:
            if overflow > 0 and _settled(entry):
                overflow -= 1
                continue
            keep.append(entry)
        record["entries"] = keep
        # The sessions list is a pointer set over the entries; once an entry is
        # gone, a session nothing points at is dropped with it so the record's
        # size is bounded by the entries, not by history.
        referenced = {e.get("session_key") for e in keep} | {
            e.get("from_session") for e in keep if e.get("type") == "escalation"
        }
        record["sessions"] = [s for s in record.get("sessions", []) if s in referenced]
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1), fsync=False)
    _PENDING_CACHE.pop(_resolve_cache_key(slug), None)
    _prime_pending_cache(slug)


def _ensure_participants(record: dict[str, Any], *, member: str, slug: str) -> None:
    parts = record.setdefault("participants", [])
    if not any(p.get("kind") == "human" for p in parts if isinstance(p, dict)):
        parts.append({"kind": "human", "id": "owner"})
    if not any(
        p.get("kind") == "member" and p.get("slug") == slug for p in parts if isinstance(p, dict)
    ):
        parts.append({"kind": "member", "slug": slug, "name": member})


def _ensure_session(record: dict[str, Any], session_key: str) -> None:
    sessions = record.setdefault("sessions", [])
    if session_key and session_key not in sessions:
        sessions.append(session_key)


def record_escalation(
    slug: str,
    *,
    member: str,
    session_key: str,
    mid: str,
    escalation_id: str,
    from_session: str,
    created_ts: str = "",
    deadline: str | None = None,
    default_action: str | None = None,
    goal: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """Add one pending escalation record pointing at its transcript row."""
    entry = {
        "type": "escalation",
        "id": escalation_id,
        "session_key": session_key,
        "mid": mid,
        "from_session": from_session,
        "state": "pending",
        "created_ts": created_ts or _now_iso(),
        "deadline": deadline,
        "default_action": default_action,
        "goal": goal,
        "options": list(options or [])[:MAX_ESCALATION_OPTIONS],
        "answered_ts": None,
    }
    with _lock_for(slug):
        record = _load_for_write(slug)
        # Count what is genuinely open: a passed deadline is settled first so a
        # backlog of defaulted/expired records never blocks a live escalation.
        # The check and the append happen under the same lock, so two racing
        # escalations cannot both squeeze in at the ceiling.
        sweep_deadlines(record)
        open_count = sum(
            1
            for e in record.get("entries", [])
            if e.get("type") == "escalation" and e.get("state") == "pending"
        )
        if open_count >= MAX_PENDING_ESCALATIONS:
            raise EscalationBacklogFull(slug, open_count)
        _ensure_participants(record, member=member, slug=slug)
        _ensure_session(record, session_key)
        record["entries"].append(entry)
        _write_conversation(slug, record)
    return entry


def sweep_deadlines(record: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Move pending records whose deadline has passed to ``defaulted`` /
    ``expired`` in place. Pure over *record*; returns whether anything moved."""
    current = now or datetime.now(timezone.utc)
    changed = False
    for entry in record.get("entries", []):
        if entry.get("type") != "escalation" or entry.get("state") != "pending":
            continue
        due = parse_ts(entry.get("deadline"))
        if due is not None and due <= current:
            entry["state"] = "defaulted" if entry.get("default_action") else "expired"
            changed = True
    return changed


def pending_escalations(record: dict[str, Any], *, now: datetime | None = None) -> list[dict]:
    """Escalations still awaiting the human, after a lazy deadline sweep."""
    sweep_deadlines(record, now=now)
    return [
        e
        for e in record.get("entries", [])
        if e.get("type") == "escalation" and e.get("state") == "pending"
    ]


def _pending_deadlines_from_disk(slug: str) -> list[tuple[str, str | None]]:
    """``(id, deadline)`` of the records stored as ``pending`` (unswept), read
    from disk. Blocking IO — callers run it off the event loop."""
    record = _parse_file(conversation_path(slug), slug)
    if record is None:
        return []
    return [
        (
            str(e.get("id") or ""),
            e.get("deadline") if isinstance(e.get("deadline"), str) else None,
        )
        for e in record["entries"]
        if e.get("type") == "escalation" and e.get("state") == "pending"
    ]


def pending_ids(slug: str, *, now: datetime | None = None) -> list[str]:
    """Ids of the escalations awaiting the human RIGHT NOW, from the in-memory
    view (no IO). The reply hook snapshots this on the event loop at the moment
    the human's row is appended, so the answer rule is evaluated against the
    pending set as it stood in transcript order — not as it stands a moment
    later on the executor, after a concurrent escalation may have landed."""
    current = now or datetime.now(timezone.utc)
    out: list[str] = []
    key = _memo_cache_key(slug)
    if key is None:
        return out  # never resolved off-loop: unprimed reads as nothing pending
    for eid, deadline in _PENDING_CACHE.get(key, ()):
        due = parse_ts(deadline)
        if due is None or due > current:
            out.append(eid)
    return out


def _prime_pending_cache(slug: str) -> None:
    """Refresh the in-memory pending view for *slug* from disk (blocking IO)."""
    try:
        _PENDING_CACHE[_resolve_cache_key(slug)] = _pending_deadlines_from_disk(slug)
    except Exception:  # noqa: BLE001 - a cache refresh must never raise into a writer
        logger.debug("pending cache prime failed for %s", slug, exc_info=True)


def prime(slug: str) -> None:
    """Load a member's pending view into memory. Blocking IO — call it off the
    event loop (``asyncio.to_thread``) once per member at slot creation or
    restore; every later change is applied by the writer that made it.

    Serialised on the same per-slug lock every writer holds across its write
    AND its cache refresh: an unlocked prime that read the file just before a
    writer's ``atomic_write`` would install the OLD pending view a beat after
    the writer installed the new one, and a free-text reply judged against that
    stale view would leave the fresh escalation pending. (``_prime_pending_cache``
    itself stays lock-free because ``_write_conversation`` calls it while the
    lock is already held — ``threading.Lock`` is not re-entrant.)"""
    with _lock_for(slug):
        _prime_pending_cache(slug)


def needs_you(slug: str, *, now: datetime | None = None) -> bool:
    """Whether the member has at least one escalation awaiting the human.

    Memory-only: this runs inside the slot projection on the event loop, on
    every sidebar push, so it must not stat or read a file. The view is kept
    current by the writers (``record_escalation`` and ``mark_answered``
    both refresh it after their write) and primed by
    :func:`prime` when a member slot is created or restored. A slug that was
    never primed reads as ``False`` until a writer or a prime touches it.

    A passed deadline clears it without a write (the file is updated the next
    time the record is written for another reason, or by :func:`mark_answered`).
    """
    try:
        return bool(pending_ids(slug, now=now))
    except Exception:  # noqa: BLE001 - a projection must never fail on derived state
        logger.debug("needs_you derivation failed for %s", slug, exc_info=True)
        return False


def mark_answered(
    slug: str,
    *,
    escalation_id: str | None = None,
    escalation_ids: list[str] | None = None,
    candidates: list[str] | None = None,
    answered_ts: str = "",
    now: datetime | None = None,
) -> int:
    """The human replied in the conversation. Which record that answers:

    * a reply carrying an ``escalation_id`` (an option chip) answers exactly
      that record, if it is still pending; a reply carrying several
      (``escalation_ids`` — chip replies merged into one row by the queue
      drain) answers each of them;
    * a reply without one (typed text) answers the pending record only when
      EXACTLY ONE is pending — with none or several it answers nothing, so an
      unrelated message cannot silently retire N open decisions;
    * a record whose deadline has already passed is swept to
      ``defaulted``/``expired`` first and is never answered late.

    ``candidates`` is the set of ids that were pending when the reply row was
    appended (:func:`pending_ids`, snapshotted on the event loop). It is what
    the free-text rule counts, so a record that landed on the executor between
    the append and this call is neither counted nor answered — the index then
    agrees with the transcript order the chat projection reads. Without a
    snapshot the rule falls back to the records pending now.

    The chat projection applies the same rule client-side, so the card and the
    index agree without a round trip. Also persists any deadline transitions
    found on the way. Returns the number of records moved to ``answered``; a
    conversation with nothing to change is left untouched (no write).
    """
    with _lock_for(slug):
        record = _load_for_write(slug)
        swept = sweep_deadlines(record, now=now)
        pending = [
            e
            for e in record.get("entries", [])
            if e.get("type") == "escalation" and e.get("state") == "pending"
        ]
        if candidates is not None:
            allowed = set(candidates)
            pending = [e for e in pending if e.get("id") in allowed]
        else:
            # Unprimed fallback (no on-loop snapshot): stand in transcript
            # order by hand. A record CREATED after the reply row — a second
            # escalation that landed on the executor during the forced save —
            # was not pending when the human replied, so the free-text rule
            # must not count it: otherwise the reply that should answer the
            # one open record sees two pending and answers neither, leaving the
            # first permanently pending. Mirrors ``reconcile_with_transcript``
            # (pending AT that point of the transcript). A record with no
            # ``created_ts`` predates this field and is kept.
            reply_cutoff = now or datetime.now(timezone.utc)

            def _created_at_or_before(entry: dict[str, Any]) -> bool:
                made = parse_ts(entry.get("created_ts"))
                return made is None or made <= reply_cutoff

            pending = [e for e in pending if _created_at_or_before(e)]
        targets: list[dict[str, Any]]
        named = [
            i for i in ([escalation_id] if escalation_id else []) + list(escalation_ids or []) if i
        ]
        if named:
            wanted = set(named)
            targets = [e for e in pending if e.get("id") in wanted]
        elif len(pending) == 1:
            targets = pending
        else:
            targets = []
        stamp = answered_ts or _now_iso(now)
        for entry in targets:
            entry["state"] = "answered"
            entry["answered_ts"] = stamp
        if targets or swept:
            _write_conversation(slug, record)
        return len(targets)


def retract_escalation(slug: str, escalation_id: str) -> bool:
    """Remove a record whose transcript row never materialised.

    The escalation path writes the index BEFORE it surfaces the card (so a fast
    reply cannot race the record); if the append then fails, this is the
    compensation — without it a no-deadline record would keep ``needs_you`` lit
    for a card nobody can see. Returns whether a record was removed.
    """
    with _lock_for(slug):
        record, state = _load_index(slug)
        if state == "unreadable":
            return False  # nothing legible to retract from; never overwrite it
        before = len(record["entries"])
        record["entries"] = [
            e
            for e in record["entries"]
            if not (e.get("type") == "escalation" and e.get("id") == escalation_id)
        ]
        if len(record["entries"]) == before:
            return False
        _write_conversation(slug, record)
        return True


#: How old a pending record must be before a missing transcript row makes it an
#: orphan. The escalation path writes the index BEFORE it appends the card, and
#: the append is a loop hop away from that write; a record younger than this is
#: presumed in flight, never swept — it is *deferred*, and the caller keeps
#: coming back until nothing is deferred.
ORPHAN_GRACE_SECS = 120


def _row_named_ids(meta: Any) -> list[str]:
    if not isinstance(meta, dict):
        return []
    out: list[str] = []
    one = meta.get("escalation_id")
    if isinstance(one, str) and one:
        out.append(one)
    many = meta.get("escalation_ids")
    if isinstance(many, list):
        out.extend(i for i in many if isinstance(i, str) and i)
    return out


def reconcile_with_transcript(
    slug: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    session_key: str = "",
    member: str = "",
) -> dict[str, Any]:
    """Recovery: make the index agree with the member's transcript, both ways.

    Consistency between this index and the transcript runs in one direction —
    the transcript is the truth, the index a thin projection of it — so on
    restore the projection is re-derived from *rows* (the transcript in order,
    persisted rows plus any live rows not yet flushed). When the index is
    missing or unreadable, the projection is REBUILT: every card row becomes a
    pending record again (``session_key``/``member`` name the thread it lives
    in) before the replay below answers what the transcript answers.

    * **orphans** — a pending record whose card row is not in *rows* and that is
      older than :data:`ORPHAN_GRACE_SECS` (the gateway exited between the index
      write and the slot's flush) moves to ``retracted`` (``retracted_reason:
      orphan``). A younger one is *deferred*: its append may simply not have
      happened yet.
    * **durable answers** — a pending record whose card row IS present and that
      a later ``user`` row with ``meta.human_reply`` answers under the same rule
      :func:`mark_answered` applies live (a named ``escalation_id`` /
      ``escalation_ids`` answers those records; free text answers the record
      only when it is the ONLY one pending at that point of the transcript)
      moves to ``answered`` — covering a gateway exit between the reply's
      transcript save and the live hook's index write.

    Replies are replayed first, each judged against the deadline as of its own
    row timestamp; only then are still-unresolved deadlines swept. Returns ``{"retracted":
    [...], "answered": [...], "deferred": n}``; the caller treats ``deferred >
    0`` as "not done yet" and reconciles again on its next read.
    """
    current = now or datetime.now(timezone.utc)
    position: dict[str, int] = {}
    for i, row in enumerate(rows):
        mid = (row.get("meta") or {}).get("mid") if isinstance(row.get("meta"), dict) else None
        if isinstance(mid, str) and mid and mid not in position:
            position[mid] = i
    with _lock_for(slug):
        record, index_state = _load_index(slug)
        # Order matters: transcript replies are replayed FIRST, each judged
        # against its record's deadline as of the reply's own timestamp, and
        # only then are the deadlines of whatever is still unresolved swept. A
        # timely reply the crash kept out of the index must never be recorded as
        # ``defaulted`` because recovery happened to run after the deadline.
        changed = False
        retracted: list[str] = []
        deferred = 0
        pending: list[dict[str, Any]] = []
        rebuilt: list[str] = []
        # The index is a projection of the transcript. When there is NO usable
        # projection — the file is unreadable (torn write, hand edit) or gone
        # while the transcript still holds cards — rebuild every card as a REAL
        # pending record from the row's own meta, then let the replay below
        # answer and the sweep settle them exactly as it would have. Writers
        # refuse an unreadable file (``_load_for_write``), so this rebuild is
        # what repairs it. With a readable index, a card it does not hold is
        # an evicted SETTLED one and is only a counting phantom (below).
        index_is_truth = index_state == "ok"
        if not index_is_truth:
            for row in rows:
                if row.get("role") != ESCALATION_ROW_ROLE:
                    continue
                rmeta = row.get("meta")
                if not isinstance(rmeta, dict):
                    continue
                eid = rmeta.get("escalation_id")
                mid = rmeta.get("mid")
                if not (isinstance(eid, str) and eid and isinstance(mid, str) and mid in position):
                    continue
                if any(e.get("id") == eid for e in record["entries"]):
                    continue
                stamp = row.get("ts") if isinstance(row.get("ts"), str) and row.get("ts") else None
                raw_options_val = rmeta.get("options")
                raw_options: list[Any] = (
                    raw_options_val if isinstance(raw_options_val, list) else []
                )
                entry = {
                    "type": "escalation",
                    "id": eid,
                    "session_key": session_key,
                    "mid": mid,
                    "from_session": (
                        rmeta.get("from_session")
                        if isinstance(rmeta.get("from_session"), str)
                        else ""
                    ),
                    "state": "pending",
                    "created_ts": (
                        rmeta.get("created_ts")
                        if isinstance(rmeta.get("created_ts"), str) and rmeta.get("created_ts")
                        else (stamp or _now_iso(current))
                    ),
                    "deadline": (
                        rmeta.get("deadline") if isinstance(rmeta.get("deadline"), str) else None
                    ),
                    "default_action": (
                        rmeta.get("default_action")
                        if isinstance(rmeta.get("default_action"), str)
                        else None
                    ),
                    "goal": rmeta.get("goal") if isinstance(rmeta.get("goal"), str) else None,
                    "options": [o for o in raw_options if isinstance(o, str)][
                        :MAX_ESCALATION_OPTIONS
                    ],
                    "answered_ts": None,
                }
                record["entries"].append(entry)
                if member or session_key:
                    _ensure_participants(record, member=member or slug, slug=slug)
                    _ensure_session(record, session_key)
                rebuilt.append(eid)
                changed = True
            if rebuilt:
                logger.warning(
                    "conversation index for %s was %s; rebuilt %d escalation record(s) "
                    "from the transcript",
                    slug,
                    index_state,
                    len(rebuilt),
                )
        # ``answered`` records whose card row is still in the transcript are
        # re-derived too: the reply that answered them may have been REWOUND
        # (a rewind rewrites the transcript, never this index). They are
        # provisionally reopened and must find their reply again in the replay
        # below; one that does not is genuinely open once more. Keyed by id so a
        # reply that re-answers with the same stamp leaves the record untouched.
        provisional: dict[str, tuple[str, Any]] = {}
        for entry in record.get("entries", []):
            if entry.get("type") != "escalation":
                continue
            state = entry.get("state")
            if state == "answered" and entry.get("mid") in position:
                provisional[str(entry.get("id", ""))] = ("answered", entry.get("answered_ts"))
                entry["state"] = "pending"
                pending.append(entry)
                continue
            if state != "pending":
                continue
            if entry.get("mid") in position:
                pending.append(entry)
                continue
            created = parse_ts(entry.get("created_ts"))
            if created is not None and (current - created).total_seconds() < ORPHAN_GRACE_SECS:
                deferred += 1
                continue
            entry["state"] = "retracted"
            entry["retracted_reason"] = "orphan"
            retracted.append(str(entry.get("id", "")))
            changed = True
        # Cards the transcript still holds but the index no longer does: the
        # size cap evicts SETTLED entries oldest-first while their card rows
        # live on. They were candidates at their positions when the human
        # replied, so the replay must see them too — otherwise a typed reply
        # that was ambiguous (two open cards) reads as unambiguous against the
        # one card the index kept and falsely answers a reopened record. They
        # take part in candidate counting only: phantoms are never written.
        known_ids = {
            str(e.get("id", "")) for e in record.get("entries", []) if e.get("type") == "escalation"
        }
        for row in rows:
            if row.get("role") != ESCALATION_ROW_ROLE:
                continue
            rmeta = row.get("meta")
            if not isinstance(rmeta, dict):
                continue
            eid = rmeta.get("escalation_id")
            mid = rmeta.get("mid")
            if not (isinstance(eid, str) and eid and eid not in known_ids):
                continue
            if not (isinstance(mid, str) and mid in position):
                continue
            known_ids.add(eid)
            pending.append(
                {
                    "type": "escalation",
                    "id": eid,
                    "mid": mid,
                    "state": "pending",
                    "deadline": rmeta.get("deadline"),
                    "_phantom": True,
                }
            )
        answered: list[str] = []
        if pending:
            for i, row in enumerate(rows):
                if row.get("role") != "user":
                    continue
                meta = row.get("meta")
                if not isinstance(meta, dict) or meta.get("human_reply") is not True:
                    continue
                stamp = row.get("ts") if isinstance(row.get("ts"), str) and row.get("ts") else None
                at = parse_ts(stamp) or current
                # Pending at THAT point of the transcript, and not yet past its
                # deadline as of the reply — a late reply answers nothing, the
                # same rule the live path applies.
                candidates = []
                for e in pending:
                    if e.get("state") != "pending" or position[e["mid"]] >= i:
                        continue
                    due = parse_ts(e.get("deadline"))
                    if due is not None and due <= at:
                        continue
                    candidates.append(e)
                if not candidates:
                    continue
                named = set(_row_named_ids(meta))
                if named:
                    targets = [e for e in candidates if e.get("id") in named]
                elif len(candidates) == 1:
                    targets = candidates
                else:
                    targets = []
                for entry in targets:
                    new_ts = stamp or _now_iso(current)
                    entry["state"] = "answered"
                    entry["answered_ts"] = new_ts
                    if entry.get("_phantom"):
                        continue  # counted as a candidate; never part of the record
                    eid = str(entry.get("id", ""))
                    if provisional.get(eid) == ("answered", new_ts):
                        continue  # the same reply, still there: nothing changed
                    answered.append(eid)
                    changed = True
        reopened: list[str] = []
        for entry in pending:
            eid = str(entry.get("id", ""))
            if eid in provisional and entry.get("state") == "pending":
                # Was answered; the transcript no longer holds the reply.
                entry["answered_ts"] = None
                reopened.append(eid)
                changed = True
        if sweep_deadlines(record, now=current):
            changed = True
        if changed:
            _write_conversation(slug, record)
        return {
            "retracted": retracted,
            "answered": answered,
            "reopened": reopened,
            "rebuilt": rebuilt,
            "deferred": deferred,
        }


def is_primed(slug: str) -> bool:
    """Whether :func:`prime` (or a writer) has loaded *slug*'s pending view into
    memory. An unprimed view is not an EMPTY view: a reader that would treat
    "nothing cached" as "nothing pending" must fall back to the file instead."""
    key = _memo_cache_key(slug)
    return key is not None and key in _PENDING_CACHE


def public_view(record: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """The index as the dashboard reads it: swept, with ``needs_you`` derived."""
    pending = pending_escalations(record, now=now)
    return {
        "conversation_id": record.get("conversation_id", ""),
        "participants": list(record.get("participants", [])),
        "sessions": list(record.get("sessions", [])),
        "entries": list(record.get("entries", [])),
        "needs_you": bool(pending),
        "pending_escalations": len(pending),
    }


def invalidate_cache(slug: str | None = None) -> None:
    """Test hook / explicit cache drop."""
    if slug is None:
        _PENDING_CACHE.clear()
        _CACHE_KEY_BY_SLUG.clear()
    else:
        memo = _CACHE_KEY_BY_SLUG.pop(slug, None)
        _PENDING_CACHE.pop(memo[1] if memo else _resolve_cache_key(slug), None)


__all__ = [
    "CONVERSATION_FILE_NAME",
    "ESCALATION_ID_RE",
    "ESCALATION_ROW_ROLE",
    "EscalationBacklogFull",
    "IndexUnreadable",
    "MAX_DEADLINE_SECS",
    "MAX_ESCALATION_OPTIONS",
    "MAX_PENDING_ESCALATIONS",
    "MIN_DEADLINE_SECS",
    "ORPHAN_GRACE_SECS",
    "conversation_id",
    "conversation_path",
    "index_unreadable",
    "is_primed",
    "mark_answered",
    "needs_you",
    "new_escalation_id",
    "parse_ts",
    "pending_escalations",
    "prime",
    "public_view",
    "read_conversation",
    "reconcile_with_transcript",
    "record_escalation",
    "resolve_deadline",
    "sweep_deadlines",
]
