"""Compile explicit creation feedback into accepted-element locks.

The canonical feedback and lock records live in :mod:`creation_review.models`.
This module contains only the small amount of service logic needed to turn
structured feedback into those records. It deliberately does not interpret
arbitrary natural language: the four compatibility phrases below are the
complete, bounded shorthand accepted for callers that predate structured
feedback directives.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from .models import (
    AcceptedElementLock,
    CreationFeedback,
    CreationReviewModel,
    FeedbackDirective,
    PaletteFeedback,
    ProcessingFeedback,
    RoleFeedback,
    SectionFeedback,
)


MAX_FEEDBACK_TEXT = 1024
MAX_FEEDBACK_ITEMS = 64
MAX_FEEDBACK_LOCKS = 128
MAX_ID = 128

FeedbackSource = Literal[
    "user_explicit",
    "connected_ai_interpretation",
    "bounce_measurement",
    "system_default",
]
FeedbackVerdict = Literal["approved", "accepted", "rejected", "needs_revision", "neutral"]
ApprovalLevel = Literal["none", "element", "section", "overall", "final"]
LockKind = Literal[
    "sound_assignment",
    "note_content",
    "rhythm",
    "register",
    "processing",
    "level",
    "section_placement",
    "role_identity",
]
LockTarget = Literal[
    "overall",
    "section",
    "role",
    "palette_assignment",
    "composition_part",
    "drum_role",
    "processing_goal",
]

# Keep the historical name as an alias, not a second public model.
FeedbackLock = AcceptedElementLock
# Backwards-compatible helper name; the actual directive/feedback contracts
# are canonical models, not local subclasses.
FeedbackModel = CreationReviewModel


def feedback_source_priority(source: FeedbackSource | str) -> int:
    """Return the authority rank for one feedback source."""

    return {
        "user_explicit": 400,
        "connected_ai_interpretation": 200,
        "bounce_measurement": 100,
        "system_default": 0,
    }.get(str(source), 0)


def _field(value: Any, name: str, default: Any = ()) -> Any:
    raw = getattr(value, name, default)
    return default if raw is None else raw


_FEEDBACK_SOURCES = frozenset(
    {
        "user_explicit",
        "connected_ai_interpretation",
        "bounce_measurement",
        "system_default",
    }
)


def _feedback_source(feedback: Any) -> FeedbackSource:
    """Return the source that owns a feedback record.

    Nested directives are untrusted transport data.  Their ``source`` field
    must never be able to elevate (or otherwise replace) the source of the
    enclosing ``CreationFeedback`` record.
    """

    value = str(_field(feedback, "source", "system_default"))
    return value if value in _FEEDBACK_SOURCES else "system_default"  # type: ignore[return-value]


def _directive_key(directive: Any) -> tuple[str, str | None, str | None, str | None]:
    return (
        str(_field(directive, "target", "overall")),
        _field(directive, "section_id", None),
        _field(directive, "role_id", None),
        _field(directive, "assignment_id", None),
    )


def _iter_group(value: Any) -> tuple[Any, ...]:
    if value is None or isinstance(value, str):
        return ()
    if isinstance(value, Mapping):
        value = tuple(value.values())
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _iter_directives(feedback: Any) -> tuple[Any, ...]:
    rows: list[Any] = []
    rows.extend(_iter_group(_field(feedback, "preserve_directives", ())))
    rows.extend(_iter_group(_field(feedback, "replacement_directives", ())))
    for group in (
        "section_feedback",
        "role_feedback",
        "palette_feedback",
        "arrangement_feedback",
        "processing_feedback",
    ):
        rows.extend(_iter_group(getattr(feedback, group, ())))
    return tuple(rows)


def _exact_compatibility_directive(
    feedback: Any,
    text: str,
    *,
    role_id: str | None = None,
    section_id: str | None = None,
) -> FeedbackDirective | None:
    """Recognize the intentionally tiny legacy phrase set."""

    normalized = re.sub(r"\s+", " ", text.strip().casefold())
    mapping: tuple[str, tuple[LockKind, ...], tuple[LockKind, ...]] | None = None
    if normalized in {"keep chords", "keep the chords", "chords are good"}:
        mapping = ("keep chords", ("sound_assignment", "note_content"), ())
        role_id = role_id or "main_chords"
    elif normalized in {"sub is good", "keep sub", "keep the sub"}:
        mapping = (
            "sub is good",
            ("sound_assignment", "note_content", "register", "role_identity"),
            (),
        )
        role_id = role_id or "sub_bass"
    elif normalized in {"keep melody, change the sound", "keep melody change sound"}:
        mapping = ("keep melody, change the sound", ("note_content",), ("sound_assignment",))
        role_id = role_id or "main_lead"
    elif normalized in {"keep the sound, rewrite the melody", "keep sound rewrite melody"}:
        mapping = ("keep the sound, rewrite the melody", ("sound_assignment",), ("note_content",))
        role_id = role_id or "main_lead"
    if mapping is None:
        return None
    return FeedbackDirective(
        directive_id=f"compat-{hashlib.sha256(normalized.encode()).hexdigest()[:16]}",
        text=mapping[0],
        target="section" if section_id else "role" if role_id else "overall",
        section_id=section_id,
        role_id=role_id,
        lock_kinds=mapping[1],
        release_lock_kinds=mapping[2],
        preserve=True,
        source=_feedback_source(feedback),
    )


def _normalize_directive(feedback: Any, directive: Any, index: int) -> FeedbackDirective | None:
    normalized: FeedbackDirective | None
    if isinstance(directive, FeedbackDirective):
        normalized = directive
    elif isinstance(directive, Mapping):
        data = dict(directive)
        data.setdefault("directive_id", f"directive-{index + 1}")
        data.setdefault("text", data.get("bounded_note", "structured feedback"))
        try:
            normalized = FeedbackDirective.model_validate(data, strict=False)
        except Exception:
            normalized = None
    elif isinstance(directive, str):
        normalized = _exact_compatibility_directive(feedback, directive)
    else:
        text = getattr(directive, "text", None) or getattr(directive, "directive", None)
        normalized = (
            _exact_compatibility_directive(
                feedback,
                text,
                role_id=getattr(directive, "role_id", None),
                section_id=getattr(directive, "section_id", None),
            )
            if isinstance(text, str)
            else None
        )
    if normalized is None:
        return None
    # The parent record is the only authoritative provenance boundary.  This
    # prevents a nested mapping/object from claiming ``user_explicit`` when
    # the connected AI or a measurement actually supplied the feedback.
    return normalized.model_copy(update={"source": _feedback_source(feedback)})


def _canonical_lock(value: Any) -> AcceptedElementLock | None:
    if isinstance(value, AcceptedElementLock):
        return value
    try:
        return AcceptedElementLock.model_validate(value, strict=False)
    except Exception:
        return None


def _bind_lock_provenance(
    lock: AcceptedElementLock,
    *,
    feedback_id: str,
    source: FeedbackSource,
) -> AcceptedElementLock:
    """Bind a supplied lock to its enclosing feedback record.

    A lock transported inside non-user feedback may remain useful as a
    connected-AI suggestion, but it cannot carry user-explicit authority.
    The parent source is therefore always authoritative for the bound record.
    """

    return lock.model_copy(
        update={
            "feedback_id": feedback_id,
            "source": source,
            "explicit": bool(lock.explicit) and source == "user_explicit",
        }
    )


def _lock_key(
    lock: AcceptedElementLock,
    kind: str | None = None,
) -> tuple[str, str | None, str | None, str | None, str]:
    lock_kind = kind or (lock.lock_types[0] if lock.lock_types else "note_content")
    target = {
        "assignment": "palette_assignment",
        "processing": "processing_goal",
    }.get(lock.scope, lock.scope)
    return (target, lock.section_id, lock.role_id, lock.target_id, lock_kind)


def _scope_for_directive(directive: FeedbackDirective) -> str:
    if directive.target == "palette_assignment":
        return "assignment"
    if directive.target in {"composition_part", "drum_role"}:
        return "role"
    if directive.target == "processing_goal":
        return "processing"
    return directive.target


def _feedback_record_directives(feedback: Any) -> tuple[FeedbackDirective, ...]:
    """Derive preserve directives from canonical boolean fields."""

    rows: list[FeedbackDirective] = []
    sequence = 0

    def add(
        *,
        target: LockTarget,
        text: str,
        section_id: str | None = None,
        role_id: str | None = None,
        assignment_id: str | None = None,
        lock_kinds: Sequence[LockKind] = (),
    ) -> None:
        nonlocal sequence
        sequence += 1
        rows.append(
            FeedbackDirective(
                directive_id=f"feedback-{_field(feedback, 'feedback_id', 'feedback')}-{sequence}",
                text=text,
                target=target,
                section_id=section_id,
                role_id=role_id,
                assignment_id=assignment_id,
                lock_kinds=tuple(lock_kinds),
                preserve=True,
                source=str(_field(feedback, "source", "user_explicit")),  # type: ignore[arg-type]
            )
        )

    for item in _iter_group(getattr(feedback, "section_feedback", ())):
        if isinstance(item, (FeedbackDirective, SectionFeedback)):
            # Section acceptance alone does not accept every sound or note in
            # the section. Nested locks are passed through separately.
            continue
        if getattr(item, "lock_kinds", ()):
            add(
                target="section",
                text=str(getattr(item, "note", None) or getattr(item, "directive", "structured section feedback")),
                section_id=getattr(item, "section_id", None),
                lock_kinds=tuple(getattr(item, "lock_kinds", ())),
            )
    for item in _iter_group(getattr(feedback, "role_feedback", ())):
        if isinstance(item, FeedbackDirective):
            continue
        if isinstance(item, RoleFeedback):
            kinds: list[LockKind] = []
            if item.keep_sound:
                kinds.append("sound_assignment")
            if item.keep_notes:
                kinds.append("note_content")
            if item.keep_rhythm:
                kinds.append("rhythm")
            if item.keep_register:
                kinds.append("register")
            if kinds:
                add(target="role", text=item.note or "accepted role elements", role_id=item.role_id, lock_kinds=kinds)
        elif getattr(item, "lock_kinds", ()):
            add(
                target="role",
                text=str(getattr(item, "directive", "structured role feedback")),
                role_id=getattr(item, "role_id", None),
                lock_kinds=tuple(getattr(item, "lock_kinds", ())),
            )
    for item in _iter_group(getattr(feedback, "palette_feedback", ())):
        if isinstance(item, FeedbackDirective):
            continue
        if isinstance(item, PaletteFeedback):
            if item.keep_assignment:
                add(
                    target="palette_assignment" if item.assignment_id else "role",
                    text=item.note or "accepted sound assignment",
                    role_id=item.role_id,
                    assignment_id=item.assignment_id,
                    lock_kinds=("sound_assignment",),
                )
        elif getattr(item, "lock_kinds", ()):
            add(
                target="palette_assignment" if getattr(item, "assignment_id", None) else "role",
                text=str(getattr(item, "directive", "structured palette feedback")),
                role_id=getattr(item, "role_id", None),
                assignment_id=getattr(item, "assignment_id", None),
                lock_kinds=tuple(getattr(item, "lock_kinds", ())),
            )
    for item in _iter_group(getattr(feedback, "processing_feedback", ())):
        if isinstance(item, (FeedbackDirective, ProcessingFeedback)):
            # Processing records require a nested explicit lock; no broad
            # processing acceptance is inferred from a verdict.
            continue
        if getattr(item, "lock_kinds", ()):
            add(
                target="processing_goal",
                text=str(getattr(item, "directive", "structured processing feedback")),
                role_id=getattr(item, "role_id", None),
                lock_kinds=tuple(getattr(item, "lock_kinds", ())),
            )
    return tuple(rows)


def build_feedback_locks(
    feedback: CreationFeedback | Any,
    *,
    existing: Sequence[FeedbackLock | Any] = (),
    max_locks: int = MAX_FEEDBACK_LOCKS,
) -> tuple[FeedbackLock, ...]:
    """Compile accepted structured feedback into independent canonical locks.

    User feedback has the highest authority. Within one source, later
    directives win for the same target and lock kind. Existing locks are
    retained, including released records, so a later explicit release does
    not erase the audit trail.
    """

    if type(max_locks) is not int or not 1 <= max_locks <= MAX_FEEDBACK_LOCKS:
        raise ValueError(f"max_locks must be within 1..{MAX_FEEDBACK_LOCKS}")
    session_id = str(_field(feedback, "review_session_id", "review-session"))
    feedback_id = str(_field(feedback, "feedback_id", "feedback"))
    feedback_source = _feedback_source(feedback)
    raw_directives = list(_iter_directives(feedback))
    raw_directives.extend(_feedback_record_directives(feedback))
    normalized: list[FeedbackDirective] = []
    for index, directive in enumerate(raw_directives[:MAX_FEEDBACK_ITEMS]):
        item = _normalize_directive(feedback, directive, index)
        if item is not None:
            normalized.append(item)

    winners: dict[tuple[str, str | None, str | None, str | None, LockKind], tuple[int, FeedbackDirective]] = {}
    releases: list[FeedbackDirective] = []
    for index, directive in enumerate(normalized):
        if directive.release_lock_kinds:
            releases.append(directive)
        for kind in directive.lock_kinds:
            key = (*_directive_key(directive), kind)
            score = feedback_source_priority(directive.source) * 10_000 + index
            prior = winners.get(key)
            if prior is None or score >= prior[0]:
                winners[key] = (score, directive)

    all_existing: list[AcceptedElementLock] = []
    # Canonical nested locks and top-level accepted locks are records, not
    # prose, so no artistic interpretation is done.  Their provenance is
    # still rebound to this enclosing feedback record at the trust boundary.
    supplied_locks: list[Any] = list(_iter_group(getattr(feedback, "accepted_locks", ())))
    for group in (
        "section_feedback",
        "role_feedback",
        "palette_feedback",
        "processing_feedback",
    ):
        for record in _iter_group(getattr(feedback, group, ())):
            supplied_locks.extend(_iter_group(getattr(record, "locks", ())))
    for item in supplied_locks:
        lock = _canonical_lock(item)
        if lock is not None and lock.lock_id not in {row.lock_id for row in all_existing}:
            all_existing.append(
                _bind_lock_provenance(
                    lock,
                    feedback_id=feedback_id,
                    source=feedback_source,
                )
            )
    for item in existing[:max_locks]:
        lock = _canonical_lock(item)
        if lock is not None and lock.lock_id not in {row.lock_id for row in all_existing}:
            all_existing.append(lock)

    # Releases are represented as released records and suppress a new winner
    # for that exact target/kind.
    for directive in releases:
        if feedback_source_priority(directive.source) < feedback_source_priority("user_explicit"):
            continue
        target_key = _directive_key(directive)
        for kind in directive.release_lock_kinds:
            winners.pop((*target_key, kind), None)
            for index, lock in enumerate(all_existing):
                if any(_lock_key(lock, lock_kind) == (*target_key, kind) for lock_kind in lock.lock_types) and not lock.released:
                    all_existing[index] = lock.model_copy(
                        update={
                            "released": True,
                            "released_by_feedback_id": feedback_id,
                            "released_by_source": feedback_source,
                        }
                    )

    existing_keys = {
        _lock_key(lock, kind)
        for lock in all_existing
        if not lock.released
        for kind in lock.lock_types
    }
    result = list(all_existing)
    for key, (_score, directive) in sorted(winners.items(), key=lambda row: row[0]):
        if key in existing_keys:
            # A later explicit user decision supersedes a derived
            # interpretation for this independent lock kind. Keep prior
            # explicit locks intact so accepted decisions remain auditable.
            if directive.source == "user_explicit":
                for index, old in enumerate(result):
                    if old.released or old.explicit:
                        continue
                    if any(_lock_key(old, old_kind) == key for old_kind in old.lock_types):
                        # Retain the derived lock as an audit record and add a
                        # new user-explicit lock.  Rewriting the old record
                        # would erase its source/feedback provenance.
                        result[index] = old.model_copy(
                            update={
                                "released": True,
                                "released_by_feedback_id": feedback_id,
                                "released_by_source": feedback_source,
                            }
                        )
                        existing_keys.discard(key)
                        break
            if key in existing_keys:
                continue
        target, section_id, role_id, assignment_id, kind = key
        target_id = assignment_id
        if target in {"composition_part", "drum_role", "processing_goal"}:
            target_id = role_id
        payload = {"session": session_id, "target": key, "feedback": feedback_id}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        try:
            scope = {
                "palette_assignment": "assignment",
                "composition_part": "role",
                "drum_role": "role",
                "processing_goal": "processing",
            }.get(target, target)
            # AcceptedElementLock requires a role for the assignment scope.
            # An assignment-only compatibility record is still safely
            # targetable, so retain its target ID under role scope instead.
            if scope == "assignment" and role_id is None:
                scope = "role"
            result.append(
                AcceptedElementLock(
                    lock_id=f"lock-{digest}",
                    scope=scope,  # type: ignore[arg-type]
                    section_id=section_id,
                    role_id=role_id,
                    target_id=target_id,
                    lock_types=(kind,),
                    directive=directive.text,
                    explicit=directive.source == "user_explicit",
                    feedback_id=feedback_id,
                    source=feedback_source,
                )
            )
        except Exception:
            # Invalid compatibility targets are ignored rather than emitting a
            # malformed lock that could poison a persisted session.
            continue
    if len(result) > max_locks:
        result = sorted(result, key=lambda item: (item.lock_id, item.released))[:max_locks]
    return tuple(result)


def release_feedback_locks(
    locks: Sequence[FeedbackLock],
    *,
    feedback_id: str,
    source: FeedbackSource = "user_explicit",
    feedback_source: FeedbackSource | None = None,
    target: LockTarget = "overall",
    section_id: str | None = None,
    role_id: str | None = None,
    assignment_id: str | None = None,
    kinds: Sequence[LockKind] = (),
) -> tuple[FeedbackLock, ...]:
    """Release only explicitly named canonical lock targets and kinds."""

    if feedback_source is not None:
        if source != "user_explicit" and source != feedback_source:
            raise ValueError("source and feedback_source disagree")
        source = feedback_source
    if source not in _FEEDBACK_SOURCES:
        raise ValueError(f"unsupported feedback source: {source!r}")
    wanted = set(kinds)
    if not wanted:
        raise ValueError("an explicit release must name at least one lock kind")
    # A connected-AI interpretation or measurement may request a release, but
    # it must not be able to unlock an accepted producer decision.  The same
    # authority boundary is enforced for release directives in
    # ``build_feedback_locks``.
    if feedback_source_priority(source) < feedback_source_priority("user_explicit"):
        return tuple(_canonical_lock(item) or item for item in locks)
    scope = {
        "palette_assignment": "assignment",
        "composition_part": "role",
        "drum_role": "role",
        "processing_goal": "processing",
    }.get(target, target)
    output: list[FeedbackLock] = []
    for raw in locks:
        item = _canonical_lock(raw)
        if item is None:
            continue
        lock_target_match = item.scope == scope
        if assignment_id is not None:
            lock_target_match = lock_target_match and item.target_id == assignment_id
        match = (
            not item.released
            and lock_target_match
            and item.section_id == section_id
            and item.role_id == role_id
            and bool(wanted.intersection(item.lock_types))
        )
        output.append(
            item.model_copy(
                update={
                    "released": True,
                    "released_by_feedback_id": feedback_id,
                    "released_by_source": source,
                }
            )
            if match
            else item
        )
    return tuple(output)


def lock_covers(
    lock: FeedbackLock | Any,
    *,
    kind: LockKind,
    section_id: str | None = None,
    role_id: str | None = None,
    assignment_id: str | None = None,
) -> bool:
    """Return whether an active canonical lock protects a target."""

    item = _canonical_lock(lock)
    if item is None or item.released or kind not in item.lock_types:
        return False
    if item.scope == "section" and item.section_id not in {None, section_id}:
        return False
    if item.scope in {"role", "assignment", "composition", "processing", "arrangement"}:
        if item.role_id is not None and item.role_id.casefold() != (role_id or "").casefold():
            return False
    if item.scope == "assignment" and item.target_id is not None and item.target_id != assignment_id:
        return False
    if item.target_id is not None and item.scope != "assignment" and item.target_id not in {role_id, section_id, assignment_id}:
        return False
    return True


def accepted_lock_violations(
    locks: Sequence[FeedbackLock | Any],
    *,
    target_kind: LockKind,
    section_id: str | None = None,
    role_id: str | None = None,
    assignment_id: str | None = None,
) -> tuple[str, ...]:
    """Return stable IDs of accepted locks that block a proposed change."""

    return tuple(
        str(getattr(item, "lock_id", "lock"))
        for item in locks
        if lock_covers(
            item,
            kind=target_kind,
            section_id=section_id,
            role_id=role_id,
            assignment_id=assignment_id,
        )
    )


def feedback_authority(feedback: CreationFeedback | Any) -> int:
    """Return the source authority rank for one feedback record."""

    return feedback_source_priority(str(_field(feedback, "source", "system_default")))


__all__ = [
    "ApprovalLevel",
    "CreationFeedback",
    "FeedbackDirective",
    "FeedbackLock",
    "FeedbackModel",
    "FeedbackSource",
    "FeedbackVerdict",
    "LockKind",
    "LockTarget",
    "accepted_lock_violations",
    "build_feedback_locks",
    "feedback_authority",
    "feedback_source_priority",
    "lock_covers",
    "release_feedback_locks",
]
