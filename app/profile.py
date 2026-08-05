"""EP-18 · coach memory — the pure rules for what the coach remembers about an athlete.

Every Claude call in the app is amnesiac: the context is rebuilt each time from the payload,
the baselines and a couple of recent reports. Everything *qualitative* the system worked out
over a year — "Wednesdays after a Tuesday tempo are always bad", "intervals in the heat get
abandoned, so don't prescribe them", "the knee complains on descents, not on volume" — lived
only inside the text of individual reports and did not exist for the next call. NF-01 closed
the quantitative half (personal metric norms). This is the other half, and it's the thing a
competitor can't buy: it comes out of our own plan↔actual↔subjective↔labs loop.

Only the **rules** live here — validation, decay, eviction and the prompt block. Storage
(encrypted) is ``app.db.profile``; the weekly Claude call that proposes deltas is phase 2.

The design is dominated by one failure mode: **profile poisoning**. A single wrong
conclusion, re-confirmed by its own presence in the prompt, would quietly steer advice for
months. So:

* a fact without evidence is never stored (:func:`normalize_fact`);
* confidence DECAYS with age, so a claim that stops being re-confirmed fades out on its own;
* a contradiction lowers confidence rather than silently rewriting the fact;
* a fact the user rejects goes on a stop-list and cannot be regenerated;
* and the whole block is hard-capped, because a profile that grows without bound makes every
  call more expensive AND drowns the signal it exists to carry.
"""
import datetime as dt
import hashlib
import re
from typing import List, Optional

# Hard ceilings. These are TESTED, not agreed: an unbounded profile inflates the input
# tokens of every single call in the app, which is exactly the kind of cost drift nobody
# notices until the monthly bill.
MAX_FACTS = 25
MAX_TOKENS = 1200

MAX_FACT_CHARS = 240   # one sentence-ish; a "fact" that needs a paragraph isn't one

# Chars-per-token estimate for the ceiling check. Deliberately pessimistic (Ukrainian
# tokenizes far worse than English) — an optimistic estimate would defeat the cap.
CHARS_PER_TOKEN = 2.5

# What a fact is ABOUT. Kept small and closed: an open taxonomy drifts into free-form tags
# and stops being useful for prompt ordering.
KINDS = ("response", "constraint", "preference", "context")

# Confidence half-life: a fact not re-confirmed for this long is worth half as much when
# competing for the 25 slots. Slow on purpose — this is a coach's memory, not a news feed.
DECAY_HALFLIFE_DAYS = 120.0

# Below this effective (decayed) confidence a fact stops being carried at all — it has
# faded rather than being deleted, which is the honest outcome for something that simply
# stopped being re-observed.
MIN_EFFECTIVE_CONFIDENCE = 0.15

_KIND_LABEL = {
    "response": "реакція на навантаження",
    "constraint": "обмеження",
    "preference": "уподобання",
    "context": "контекст життя",
}


def estimate_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def fact_id(text: str) -> str:
    """A stable id derived from the statement itself, so the same claim proposed twice is
    the same fact (confirmed) rather than a duplicate — and so the stop-list can recognise a
    rejected statement no matter which week it comes back in."""
    return hashlib.sha256(_canonical(text).encode("utf-8")).hexdigest()[:12]


def _canonical(text: str) -> str:
    """Lower-cased, punctuation- and whitespace-normalised form, used only for identity.
    Without it "Коліно ниє на спусках." and "коліно ниє на спусках" are two facts, and the
    stop-list leaks."""
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip()


def _parse_date(v) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def normalize_fact(raw: dict, *, today: Optional[dt.date] = None) -> Optional[dict]:
    """Validate one incoming fact into storage shape, or ``None`` if it can't be trusted.

    A fact with **no evidence** is rejected outright — that single rule is the main defence
    against the poisoning failure mode, because it forces every remembered claim back to a
    ``report_logs`` row a human can go and read."""
    today = today or dt.date.today()
    text = (raw.get("text") or "").strip()
    if not text or len(text) > MAX_FACT_CHARS:
        return None
    kind = raw.get("kind")
    if kind not in KINDS:
        return None
    evidence = [e for e in (raw.get("evidence") or []) if isinstance(e, int)]
    if not evidence:
        return None
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        return None
    confidence = max(0.0, min(1.0, confidence))
    first_seen = _parse_date(raw.get("first_seen")) or today
    last_confirmed = _parse_date(raw.get("last_confirmed")) or today
    return {
        "id": raw.get("id") or fact_id(text),
        "text": text,
        "kind": kind,
        "confidence": round(confidence, 2),
        "first_seen": first_seen.isoformat(),
        "last_confirmed": last_confirmed.isoformat(),
        "evidence": evidence[:5],
        "pinned": bool(raw.get("pinned")),
    }


def effective_confidence(fact: dict, today: Optional[dt.date] = None) -> float:
    """Stored confidence decayed by how long it's been since the fact was last confirmed.
    A pinned fact ("this matters, keep it") does not decay — the user overrode the heuristic
    and the heuristic must not quietly override them back."""
    if fact.get("pinned"):
        return float(fact.get("confidence", 0.5))
    today = today or dt.date.today()
    last = _parse_date(fact.get("last_confirmed")) or today
    age_days = max(0, (today - last).days)
    return float(fact.get("confidence", 0.5)) * (0.5 ** (age_days / DECAY_HALFLIFE_DAYS))


def select(facts: List[dict], *, today: Optional[dt.date] = None) -> List[dict]:
    """The facts that actually travel in a prompt: strongest first, faded ones dropped,
    then cut to BOTH ceilings (count and tokens). Pinned facts sort first regardless of
    score, since pinning exists precisely to survive eviction."""
    today = today or dt.date.today()
    scored = []
    for f in facts:
        eff = effective_confidence(f, today)
        if not f.get("pinned") and eff < MIN_EFFECTIVE_CONFIDENCE:
            continue
        scored.append((1 if f.get("pinned") else 0, eff, f))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    out: List[dict] = []
    used = 0
    for _pin, _eff, f in scored[:MAX_FACTS]:
        cost = estimate_tokens(f["text"]) + 4      # + the "• [kind] " framing
        if used + cost > MAX_TOKENS:
            break
        out.append(f)
        used += cost
    return out


def to_context(facts: List[dict], *, today: Optional[dt.date] = None) -> Optional[dict]:
    """The prompt block, or ``None`` for an empty profile.

    ``None`` rather than an empty structure on purpose (an AC): a new user's prompts must be
    byte-for-byte what they are today, so the field is absent, not present-and-empty."""
    chosen = select(facts, today=today)
    if not chosen:
        return None
    return {
        "facts": [
            {"id": f["id"], "text": f["text"], "kind": f["kind"],
             "since": f["first_seen"]}
            for f in chosen
        ],
    }


def to_lines(facts: List[dict], *, today: Optional[dt.date] = None) -> List[str]:
    """Human-readable rendering, for a display surface and for debugging what the coach
    actually 'knows'."""
    return [f"• [{_KIND_LABEL.get(f['kind'], f['kind'])}] {f['text']}"
            for f in select(facts, today=today)]


def apply_delta(facts: List[dict], delta: dict, *, today: Optional[dt.date] = None,
                stoplist: Optional[List[str]] = None) -> List[dict]:
    """Merge one round of ``{add, confirm, contradict, drop}`` into the stored facts.

    A **delta**, never a rewrite: the weekly pass (phase 2) proposes changes against what is
    already known instead of regenerating the profile, so one bad week cannot wipe a year of
    accumulated observation. ``contradict`` lowers confidence rather than deleting, because a
    single contradicting week is evidence, not proof — a claim that keeps being contradicted
    decays out of the top-25 on its own.

    ``stoplist`` holds ``fact_id``s the user rejected; anything on it is silently refused,
    including a re-proposal of the same statement weeks later (the AC that "it doesn't come
    back")."""
    today = today or dt.date.today()
    blocked = set(stoplist or [])
    by_id = {f["id"]: dict(f) for f in facts}

    for raw in (delta.get("add") or []):
        f = normalize_fact(raw, today=today)
        if f is None or f["id"] in blocked:
            continue
        existing = by_id.get(f["id"])
        if existing is None:
            by_id[f["id"]] = f
        else:
            # Re-proposing a known fact is a confirmation, not a duplicate.
            existing["confidence"] = round(min(1.0, existing["confidence"] + 0.1), 2)
            existing["last_confirmed"] = today.isoformat()
            existing["evidence"] = (existing.get("evidence") or [] + f["evidence"])[:5]

    for fid in (delta.get("confirm") or []):
        f = by_id.get(fid)
        if f is not None:
            f["confidence"] = round(min(1.0, f["confidence"] + 0.15), 2)
            f["last_confirmed"] = today.isoformat()

    for fid in (delta.get("contradict") or []):
        f = by_id.get(fid)
        if f is not None:
            f["confidence"] = round(max(0.0, f["confidence"] - 0.25), 2)

    for fid in (delta.get("drop") or []):
        by_id.pop(fid, None)

    return [f for f in by_id.values() if f["id"] not in blocked]


def forget(facts: List[dict], stoplist: List[str], fact_id_or_text: str):
    """User-driven "this is not true": remove the fact AND remember the rejection, so next
    week's pass can't rediscover it. Returns ``(facts, stoplist, removed)``.

    Accepts either the short id or the statement itself, so ``/forget`` works from a copied
    line of the profile page as well as from an id."""
    target = fact_id_or_text.strip()
    ids = {f["id"] for f in facts}
    fid = target if target in ids else fact_id(target)
    remaining = [f for f in facts if f["id"] != fid]
    removed = len(remaining) != len(facts)
    if fid not in stoplist:
        stoplist = [*stoplist, fid]
    return remaining, stoplist, removed
