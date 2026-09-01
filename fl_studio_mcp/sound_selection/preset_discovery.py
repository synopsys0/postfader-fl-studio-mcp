"""Deterministic bounded preset-candidate discovery.

The live bridge may expose only one page at a time, so discovery is an
explicit policy rather than an implicit full-catalog scan.  The service can
consume an already captured :class:`PresetCatalog`, a sequence of records, or
small caller-supplied page/exact-lookup callbacks.  It never invents names and
always reports when large-catalog coverage is stratified or targeted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import AliasChoices, Field, model_validator

from .models import (
    MAX_PRESETS,
    MAX_REPORTED_PRESET_COUNT,
    SoundPresetDiscoveryCoverage,
    SoundSelectionModel,
)
from .preset_catalog import PresetCatalog, PresetPage, PresetRecord


MAX_DISCOVERY_PAGES = 32
MAX_DISCOVERY_CANDIDATES = 256
MAX_DISCOVERY_EXACT_REQUESTS = 64
DiscoveryPagePurpose = Literal[
    "current",
    "first",
    "final",
    "stratified",
    "seed",
    "targeted",
    "history",
    "anchor",
    "unknown",
]


class PresetCandidateDiscoveryPolicy(SoundSelectionModel):
    """Bounded page policy shared by all deterministic discovery calls."""

    small_catalog_threshold: int = Field(default=128, ge=1, le=4096)
    page_size: int = Field(default=64, ge=1, le=MAX_PRESETS)
    max_pages: int = Field(default=12, ge=1, le=MAX_DISCOVERY_PAGES)
    # Keep the default small-catalog threshold enumerable.  Callers may still
    # choose a smaller candidate bound; discovery then lowers the effective
    # threshold and reports stratified coverage once the bound is exceeded.
    max_candidates: int = Field(default=128, ge=1, le=MAX_DISCOVERY_CANDIDATES)
    stratified_page_count: int = Field(default=5, ge=1, le=8)
    seed_page_count: int = Field(default=2, ge=0, le=8)
    include_current: bool = True
    include_final_page: bool = True

    @model_validator(mode="after")
    def validate_budget(self) -> "PresetCandidateDiscoveryPolicy":
        return self

    @property
    def effective_small_catalog_threshold(self) -> int:
        """Largest catalog this policy can retain as a complete candidate set."""

        return min(self.small_catalog_threshold, self.max_candidates)


class PresetCandidatePage(SoundSelectionModel):
    """One bounded page observed by the discovery policy."""

    start: int = Field(default=0, ge=0, le=MAX_REPORTED_PRESET_COUNT)
    limit: int = Field(default=64, ge=1, le=MAX_PRESETS)
    reported_preset_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_REPORTED_PRESET_COUNT,
        validation_alias=AliasChoices("reported_preset_count", "preset_count"),
        serialization_alias="reported_preset_count",
    )
    presets: tuple[PresetRecord, ...] = Field(
        default=(),
        max_length=MAX_PRESETS,
        validation_alias=AliasChoices("presets", "records"),
        serialization_alias="presets",
    )
    purpose: DiscoveryPagePurpose = "unknown"
    source: str | None = Field(default=None, max_length=128)
    complete: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def validate_page(self) -> "PresetCandidatePage":
        indexes = [item.index for item in self.presets]
        if indexes != sorted(set(indexes)):
            raise ValueError("candidate page records must be ordered and uniquely indexed")
        if len(self.presets) > self.limit:
            raise ValueError("candidate page exceeds its limit")
        if any(item.index < self.start or item.index >= self.start + self.limit for item in self.presets):
            raise ValueError("candidate page records fall outside the requested page")
        if self.reported_preset_count is not None and any(
            item.index >= self.reported_preset_count for item in self.presets
        ):
            raise ValueError("candidate page record exceeds reported preset count")
        return self

    @property
    def page_index(self) -> int:
        return self.start // self.limit

    @property
    def records(self) -> tuple[PresetRecord, ...]:
        return self.presets


PresetCandidateCoverage = SoundPresetDiscoveryCoverage


class PresetCandidateSet(SoundSelectionModel):
    """Bounded deterministic preset records plus coverage evidence."""

    presets: tuple[PresetRecord, ...] = Field(
        default=(),
        max_length=MAX_DISCOVERY_CANDIDATES,
        validation_alias=AliasChoices("presets", "records", "candidates"),
        serialization_alias="presets",
    )
    pages: tuple[PresetCandidatePage, ...] = Field(default=(), max_length=MAX_DISCOVERY_PAGES)
    coverage: PresetCandidateCoverage
    warnings: tuple[str, ...] = Field(default=(), max_length=32)

    @property
    def records(self) -> tuple[PresetRecord, ...]:
        return self.presets

    @property
    def candidates(self) -> tuple[PresetRecord, ...]:
        return self.presets

    @property
    def complete(self) -> bool:
        return self.coverage.coverage_mode == "complete"


def _as_record(value: Any, *, default_index: int | None = None) -> PresetRecord | None:
    if isinstance(value, PresetRecord):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        if "index" not in data and default_index is not None:
            data["index"] = default_index
        if "name" not in data:
            for alias in ("preset_name", "preset"):
                if alias in data:
                    data["name"] = data[alias]
                    break
        try:
            return PresetRecord.model_validate(data)
        except Exception:
            return None
    name = getattr(value, "name", None)
    index = getattr(value, "index", default_index)
    if index is None:
        return None
    try:
        return PresetRecord(index=index, name=name)
    except Exception:
        return None


def _records_from(value: Any, *, start: int = 0) -> tuple[PresetRecord, ...]:
    if isinstance(value, PresetCandidatePage):
        return value.presets
    if isinstance(value, PresetPage):
        return tuple(item for item in value.presets if not item.is_blank)
    if isinstance(value, PresetCatalog):
        return tuple(item for item in value.presets if not item.is_blank)
    if isinstance(value, Mapping):
        if "name" in value or "preset_name" in value:
            record = _as_record(value, default_index=start)
            return () if record is None or record.is_blank else (record,)
        raw_rows: Any = value.get(
            "presets", value.get("records", value.get("candidates", ()))
        )
        if isinstance(raw_rows, Mapping):
            raw_rows = tuple(raw_rows.values())
        return _records_from(raw_rows, start=start)
    # Track B pages and small test doubles expose a bounded presets
    # attribute without using the bridge-free PresetPage model. Normalize
    # that surface rather than treating the page object itself as one row.
    page_rows = getattr(value, "presets", None)
    if page_rows is not None and not isinstance(value, (str, bytes)):
        return _records_from(page_rows, start=start)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rows: list[PresetRecord] = []
        for position, item in enumerate(value):
            record = _as_record(item, default_index=start + position)
            if record is not None and not record.is_blank:
                rows.append(record)
        return tuple(rows)
    record = _as_record(value, default_index=start)
    return () if record is None or record.is_blank else (record,)


def _reported_count(value: Any) -> int | None:
    if isinstance(value, (PresetCatalog, PresetPage, PresetCandidatePage)):
        return value.reported_preset_count
    if isinstance(value, Mapping):
        raw = value.get("reported_preset_count", value.get("preset_count"))
        return raw if type(raw) is int and raw >= 0 else None
    raw = getattr(value, "reported_preset_count", getattr(value, "preset_count", None))
    return raw if type(raw) is int and raw >= 0 else None


def _page_complete(value: Any, *, count: int | None, start: int, limit: int) -> bool:
    """Tell whether one adapter page proves its covered slice is complete."""

    if isinstance(value, (PresetCandidatePage, PresetPage)):
        return value.complete if isinstance(value, PresetCandidatePage) else (
            not value.partial
            and not value.truncated
            and (
                value.reported_preset_count is None
                or len(_records_from(value)) >= value.reported_preset_count
            )
        )
    explicit = getattr(value, "complete", None)
    if explicit is True:
        return True
    if explicit is False:
        return False
    partial = bool(getattr(value, "partial", False))
    truncated = bool(getattr(value, "truncated", False))
    has_more = getattr(value, "has_more", None)
    if partial or truncated or has_more is True:
        return False
    if count is None:
        return False
    records = _records_from(value, start=start)
    return start + limit >= count or len(records) >= min(limit, max(0, count - start))


def _call_page(loader: Callable[..., Any], start: int, limit: int) -> Any:
    """Call common page callback spellings without requiring an adapter class."""

    attempts = (
        {"start": start, "limit": limit},
        {"page_start": start, "page_size": limit},
        {"offset": start, "limit": limit},
    )
    for kwargs in attempts:
        try:
            return loader(**kwargs)
        except TypeError:
            continue
    try:
        return loader(start, limit)
    except TypeError:
        return loader(start=start)


def _call_exact(loader: Callable[..., Any], name: str) -> Any:
    attempts = (
        {"name": name},
        {"preset_name": name},
        {"query": name},
    )
    for kwargs in attempts:
        try:
            return loader(**kwargs)
        except TypeError:
            continue
    try:
        return loader(name)
    except TypeError:
        return None


def _find_callable(source: Any, *names: str) -> Callable[..., Any] | None:
    if callable(source):
        return source
    if isinstance(source, Mapping):
        for name in names:
            callback = source.get(name)
            if callable(callback):
                return callback
    for name in names:
        callback = getattr(source, name, None)
        if callable(callback):
            return callback
    return None


def _history_values(history: Any, *, accepted: bool) -> tuple[str, ...]:
    rows: Iterable[Any] = ()
    if history is None:
        return ()
    if isinstance(history, Mapping):
        rows = history.get("records", ())
    elif hasattr(history, "records") and callable(history.records):
        rows = history.records()  # type: ignore[reportUnknownVariableType]
    elif hasattr(history, "records"):
        rows = getattr(history, "records", ())
    elif hasattr(history, "snapshot"):
        snapshot = history.snapshot()
        rows = getattr(snapshot, "records", ())
    elif isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        rows = history
    result: list[str] = []
    for row in rows:
        accepted_count = _row_value(row, "accepted_count", 0)
        rejected_count = _row_value(row, "rejected_count", 0)
        if accepted and (not isinstance(accepted_count, int) or accepted_count <= 0):
            continue
        if not accepted and (not isinstance(rejected_count, int) or rejected_count <= 0):
            continue
        name = _row_value(row, "preset_name")
        if name is None:
            name = _row_value(row, "name")
        if name is None:
            name = _row_value(row, "selected_preset")
        if isinstance(name, str) and name.strip():
            result.append(name.strip())
    return tuple(dict.fromkeys(result))


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """Read a history/anchor row from either a strict model or mapping."""

    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _anchor_values(anchors: Iterable[Any]) -> tuple[str, ...]:
    values: list[str] = []
    for row in anchors:
        name = _row_value(row, "selected_preset")
        if name is None:
            name = _row_value(row, "preset_name")
        if isinstance(name, str) and name.strip():
            values.append(name.strip())
    return tuple(dict.fromkeys(values))


def _anchor_indices(anchors: Iterable[Any]) -> tuple[int, ...]:
    values: list[int] = []
    for row in anchors:
        index = _row_value(row, "selected_preset_index")
        if index is None:
            index = _row_value(row, "preset_index")
        if type(index) is int and index >= 0:
            values.append(index)
    return tuple(dict.fromkeys(values))


def _seed_starts(count: int, policy: PresetCandidateDiscoveryPolicy, seed: int) -> tuple[int, ...]:
    if count <= policy.page_size or policy.seed_page_count <= 0:
        return ()
    page_count = max(1, (count + policy.page_size - 1) // policy.page_size)
    digest = hashlib.sha256(f"preset-discovery:{seed}:{count}".encode("ascii")).digest()
    values: list[int] = []
    for offset in range(policy.seed_page_count):
        chunk = digest[offset * 2 : offset * 2 + 2]
        page = int.from_bytes(chunk, "big") % page_count
        values.append(page * policy.page_size)
    return tuple(values)


def _collectively_covers_catalog(
    pages: Sequence[PresetCandidatePage],
    *,
    count: int | None,
    candidate_bound: int,
) -> bool:
    """Prove that bounded pages contain every usable slot in a small catalog.

    A source-level ``complete`` flag only describes the page or object it came
    from.  Live bridges commonly return a partial first page, so completion is
    inferred from the union of all loaded page indexes instead.  Blank rows are
    intentionally omitted by ``_records_from`` and therefore prevent a false
    complete claim: their names cannot be selected safely.
    """

    if count is None or count > candidate_bound or count < 0:
        return False
    observed = {item.index for page in pages for item in page.presets}
    return len(observed) == count and observed == set(range(count))


def discover_preset_candidates(
    catalog: Any = None,
    *,
    policy: PresetCandidateDiscoveryPolicy | Mapping[str, Any] | None = None,
    page_loader: Callable[..., Any] | None = None,
    exact_lookup: Callable[..., Any] | None = None,
    catalog_access: Any = None,
    requested_presets: Iterable[str] = (),
    exact_preset_names: Iterable[str] = (),
    preferred_presets: Iterable[str] = (),
    excluded_presets: Iterable[str] = (),
    accepted_presets: Iterable[str] = (),
    rejected_presets: Iterable[str] = (),
    history_accepted: Iterable[str] = (),
    history_rejected: Iterable[str] = (),
    anchors: Iterable[Any] = (),
    anchor_assignments: Iterable[Any] | None = None,
    anchor_preset_indices: Iterable[int] = (),
    history: Any = None,
    current_preset: str | None = None,
    current_preset_index: int | None = None,
    reported_preset_count: int | None = None,
    seed: int = 0,
) -> PresetCandidateSet:
    """Return a bounded, deterministic candidate set.

    ``catalog_access`` may be an object/mapping exposing ``page`` and
    ``exact`` callbacks.  Exact lookup is attempted for every requested or
    preferred name on large catalogs, making the result independent of which
    page contains that name whenever the supplied access surface supports it.
    """

    resolved_policy = policy if isinstance(policy, PresetCandidateDiscoveryPolicy) else PresetCandidateDiscoveryPolicy.model_validate(policy or {})
    if catalog_access is not None:
        page_loader = page_loader or _find_callable(
            catalog_access,
            "page",
            "page_loader",
            "load_page",
            "get_page",
            "list_page",
        )
        exact_lookup = exact_lookup or _find_callable(
            catalog_access,
            "exact",
            "exact_lookup",
            "lookup_exact",
            "find_exact",
            "lookup",
        )
        if page_loader is None and isinstance(catalog_access, Mapping):
            page_rows = catalog_access.get("pages")
            if isinstance(page_rows, Mapping):
                def _mapped_page(*, start: int = 0, limit: int = 64, **_: Any) -> Any:
                    return page_rows.get(start, page_rows.get(str(start), ()))

                page_loader = _mapped_page
            elif isinstance(page_rows, Sequence) and not isinstance(page_rows, (str, bytes)):
                def _sequence_page(*, start: int = 0, limit: int = 64, **_: Any) -> Any:
                    return tuple(page_rows[start : start + limit])

                page_loader = _sequence_page
    if page_loader is None:
        page_loader = _find_callable(
            catalog, "page", "page_loader", "load_page", "get_page", "list_page"
        )
    if exact_lookup is None:
        exact_lookup = _find_callable(
            catalog,
            "exact",
            "exact_lookup",
            "resolve_exact",
            "lookup_exact",
            "find_exact",
        )

    # A caller may supply a finite catalog as data through catalog_access
    # instead of using the positional argument. Promote only explicit
    # records/presets data; a mapping that contains page callbacks remains a
    # potentially partial access surface and must not be called complete.
    if catalog is None and isinstance(catalog_access, Mapping) and any(
        key in catalog_access for key in ("presets", "records", "candidates")
    ):
        catalog = catalog_access

    catalog_count = _reported_count(catalog)
    count = reported_preset_count if type(reported_preset_count) is int else catalog_count
    if count is None:
        count = _reported_count(catalog_access)
    if count is None and isinstance(catalog, Sequence) and not isinstance(catalog, (str, bytes)):
        count = len(catalog)
    if count is None and isinstance(catalog, (PresetCatalog, PresetPage, PresetCandidatePage)):
        count = len(_records_from(catalog))
    if count is None and isinstance(catalog, Mapping):
        data_rows = catalog.get(
            "presets", catalog.get("records", catalog.get("candidates", ()))
        )
        if isinstance(data_rows, Mapping):
            data_rows = tuple(data_rows.values())
        if isinstance(data_rows, Sequence) and not isinstance(data_rows, (str, bytes)):
            count = len(_records_from(data_rows))

    exact_names = tuple(dict.fromkeys(
        value.strip() for value in (*requested_presets, *exact_preset_names, *preferred_presets)
        if isinstance(value, str) and value.strip()
    ))
    accepted = tuple(dict.fromkeys(
        value.strip()
        for value in (
            *accepted_presets,
            *history_accepted,
            *_history_values(history, accepted=True),
        )
        if isinstance(value, str) and value.strip()
    ))
    rejected = tuple(dict.fromkeys(
        value.strip()
        for value in (
            *rejected_presets,
            *history_rejected,
            *_history_values(history, accepted=False),
        )
        if isinstance(value, str) and value.strip()
    ))
    if anchor_assignments is not None:
        anchors = (*anchors, *anchor_assignments)
    anchor_names = _anchor_values(anchors)
    anchor_indices = tuple(dict.fromkeys((*_anchor_indices(anchors), *(item for item in anchor_preset_indices if type(item) is int and item >= 0))))
    continuity_names = tuple(dict.fromkeys((*accepted, *anchor_names)))
    excluded = tuple(dict.fromkeys(
        value.strip() for value in (*excluded_presets, *rejected)
        if isinstance(value, str) and value.strip()
    ))
    excluded_keys = {item.casefold() for item in excluded}

    small_catalog_threshold = resolved_policy.effective_small_catalog_threshold
    pages: list[PresetCandidatePage] = []
    starts: list[tuple[int, DiscoveryPagePurpose]] = []
    if count is not None and count <= small_catalog_threshold:
        starts.extend(
            (start, "first")
            for start in range(0, count, resolved_policy.page_size)
        )
    elif count is not None:
        starts.append((0, "first"))
        last = max(0, count - resolved_policy.page_size)
        if resolved_policy.include_final_page:
            starts.append((last, "final"))
        # ``stratified_page_count`` includes the first/final pages when those
        # are enabled; any remaining budget is distributed across the middle
        # of the catalog.  This keeps the policy knob meaningful while still
        # guaranteeing the requested edge pages when configured.
        reserved_pages = 1 + int(resolved_policy.include_final_page)
        interior_pages = max(0, resolved_policy.stratified_page_count - reserved_pages)
        fractions = tuple(
            (position + 1) / (interior_pages + 1)
            for position in range(interior_pages)
        )
        for fraction in fractions:
            starts.append(
                (
                    max(
                        0,
                        min(
                            last,
                            int((count - 1) * fraction)
                            // resolved_policy.page_size
                            * resolved_policy.page_size,
                        ),
                    ),
                    "stratified",
                )
            )
        starts.extend((start, "seed") for start in _seed_starts(count, resolved_policy, seed))
    elif catalog is not None and isinstance(
        catalog, (PresetCatalog, PresetPage, PresetCandidatePage, Sequence, Mapping)
    ) or (catalog is not None and hasattr(catalog, "presets")):
        starts.append((0, "first"))

    # Keep page starts stable, ordered by policy intent, and inside the bound.
    seen_starts: set[int] = set()
    for start, purpose in starts:
        if start in seen_starts or len(pages) >= resolved_policy.max_pages:
            continue
        seen_starts.add(start)
        source: Any = None
        source_complete_hint = False
        if isinstance(catalog, PresetCatalog):
            source = tuple(item for item in catalog.presets if start <= item.index < start + resolved_policy.page_size)
            source_count = catalog.reported_preset_count
            source_complete_hint = catalog.complete
        elif isinstance(catalog, PresetPage) and start == catalog.start:
            source = catalog
            source_count = catalog.reported_preset_count
            source_complete_hint = (
                not catalog.partial
                and not catalog.truncated
                and (
                    catalog.reported_preset_count is None
                    or len(_records_from(catalog)) >= catalog.reported_preset_count
                )
            )
        elif isinstance(catalog, PresetCandidatePage) and start == catalog.start:
            source = catalog
            source_count = catalog.reported_preset_count
            source_complete_hint = catalog.complete
        elif (
            catalog is not None
            and hasattr(catalog, "presets")
            and start == getattr(catalog, "start", 0)
        ):
            source = catalog
            source_count = _reported_count(catalog)
            source_complete_hint = _page_complete(
                catalog,
                count=source_count,
                start=start,
                limit=resolved_policy.page_size,
            )
        elif isinstance(catalog, Sequence) and not isinstance(catalog, (str, bytes)):
            source_rows = tuple(
                sorted(
                    catalog,
                    key=lambda item: (
                        getattr(
                            item,
                            "index",
                            item.get("index", 0) if isinstance(item, Mapping) else 0,
                        ),
                        str(
                            getattr(
                                item,
                                "name",
                                item.get("name", "") if isinstance(item, Mapping) else "",
                            )
                        ),
                    ),
                )
            )
            source = source_rows[start : start + resolved_policy.page_size]
            source_count = count
            source_complete_hint = True
        elif isinstance(catalog, Mapping):
            source = catalog.get(
                "presets", catalog.get("records", catalog.get("candidates", ()))
            )
            if isinstance(source, Mapping):
                source = tuple(source.values())
            if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
                source = tuple(source)[start : start + resolved_policy.page_size]
            source_count = count
            source_complete_hint = catalog.get("complete") is True
        else:
            source_count = count
        if source is None and page_loader is not None:
            try:
                source = _call_page(page_loader, start, resolved_policy.page_size)
            except Exception:
                source = None
        records = _records_from(source, start=start)
        if source is None:
            page = PresetCandidatePage(
                start=start,
                limit=resolved_policy.page_size,
                reported_preset_count=source_count,
                purpose=purpose,
                warnings=("page could not be loaded",),
            )
        else:
            source_reported_count = _reported_count(source)
            page = PresetCandidatePage(
                start=start,
                limit=resolved_policy.page_size,
                reported_preset_count=(
                    source_reported_count
                    if source_reported_count is not None
                    else source_count
                ),
                presets=records,
                purpose=purpose,
                complete=(
                    source is not None
                    and source_complete_hint
                    and (
                        source_count is None
                        or start + resolved_policy.page_size >= source_count
                        or len(records) >= min(resolved_policy.page_size, source_count - start)
                    )
                ),
            )
        pages.append(page)

    # A full local catalog is already an exact data surface; no callback is
    # needed for targeted names.  Otherwise use the supplied exact resolver.
    targeted_rows: list[PresetRecord] = []
    exact_limit = MAX_DISCOVERY_EXACT_REQUESTS
    lookup_names = tuple(dict.fromkeys((*exact_names, *continuity_names)))
    for name in lookup_names[:exact_limit]:
        matches = tuple(
            item
            for page in pages
            for item in page.presets
            if item.name is not None and item.name.casefold() == name.casefold()
        )
        if not matches and isinstance(catalog, PresetCatalog):
            matches = tuple(item for item in catalog.presets if item.name and item.name.casefold() == name.casefold())
        if not matches and isinstance(catalog, Sequence) and not isinstance(catalog, (str, bytes)):
            matches = tuple(
                item
                for item in _records_from(catalog)
                if item.name and item.name.casefold() == name.casefold()
            )
        if not matches and catalog is not None:
            # Mapping/object data surfaces are also valid finite catalogs.
            # Search them before giving up so an exact request does not depend
            # on which sampled page happened to contain the name.
            matches = tuple(
                item
                for item in _records_from(catalog)
                if item.name and item.name.casefold() == name.casefold()
            )
        if not matches and exact_lookup is not None:
            try:
                matches = _records_from(_call_exact(exact_lookup, name))
            except Exception:
                matches = ()
        if matches:
            targeted_rows.extend(matches[:2])

    current_unresolved = False
    if current_preset and resolved_policy.include_current:
        current_rows = [
            item for page in pages for item in page.presets
            if item.name and item.name.casefold() == current_preset.casefold()
        ]
        if not current_rows:
            if type(current_preset_index) is int and current_preset_index >= 0:
                targeted_rows.append(
                    PresetRecord(
                        index=current_preset_index,
                        name=current_preset,
                        is_current=True,
                    )
                )
            else:
                # A name without a verified index is not enough to construct
                # an executable identity; do not fabricate index zero.
                current_unresolved = True

    rows: list[PresetRecord] = []
    by_identity: set[tuple[int, str | None]] = set()
    for row in (*[item for page in pages for item in page.presets], *targeted_rows):
        if row.name is None or not row.name.strip() or row.name.casefold() in excluded_keys:
            continue
        key = (row.index, row.name.casefold())
        if key in by_identity:
            continue
        by_identity.add(key)
        rows.append(row)
    # Exact user requests and known continuity anchors must survive the
    # candidate bound even when their index is near the end of a large
    # catalog.  Remaining rows are deterministic index order.
    priority_names = {
        *(_value.casefold() for _value in exact_names),
        *(value.casefold() for value in accepted),
        *(value.casefold() for value in anchor_names),
        *((current_preset.casefold(),) if isinstance(current_preset, str) else ()),
    }
    rows.sort(
        key=lambda item: (
            0
            if (
                (item.name and item.name.casefold() in priority_names)
                or item.index in anchor_indices
            )
            else 1,
            item.index,
            item.name.casefold() if item.name else "",
        )
    )
    rows = rows[: resolved_policy.max_candidates]

    def found_names(values: Iterable[str]) -> tuple[str, ...]:
        present = {item.name.casefold() for item in rows if item.name}
        return tuple(value for value in values if value.casefold() in present)

    exact_matches = found_names(exact_names)
    accepted_matches = found_names(accepted)
    anchor_matches = found_names(anchor_names)
    anchor_matches = tuple(
        dict.fromkeys(
            (
                *anchor_matches,
                *(
                    item.name
                    for item in rows
                    if item.index in anchor_indices and item.name
                ),
            )
        )
    )
    seed_matches = found_names(
        item.name
        for page in pages
        if page.purpose == "seed"
        for item in page.presets
        if item.name and item.name.casefold() not in excluded_keys
    )
    excluded_matches = tuple(
        item.name for page in pages for item in page.presets
        if item.name and item.name.casefold() in excluded_keys
    )
    omitted = 0 if count is None else max(0, count - len(rows))
    if (
        count is not None
        and count <= small_catalog_threshold
        and _collectively_covers_catalog(
            pages,
            count=count,
            candidate_bound=resolved_policy.max_candidates,
        )
        and not any(page.warnings for page in pages)
    ):
        mode: Literal["complete", "stratified", "targeted", "minimal"] = "complete"
    elif count is not None and count > small_catalog_threshold:
        # An exact resolver may be the only supported access surface.  Do not
        # call that targeted result a stratified sample when no page yielded
        # any records; coverage mode describes observed evidence, not merely
        # the pages that were attempted.
        mode = (
            "stratified"
            if any(page.presets for page in pages)
            else "targeted"
            if exact_matches or accepted_matches or anchor_matches
            else "minimal"
        )
    elif exact_matches or accepted_matches or anchor_matches:
        mode = "targeted"
    else:
        mode = "minimal"
    warnings = tuple(dict.fromkeys(item for page in pages for item in page.warnings))
    limitations: list[str] = []
    if mode == "stratified":
        limitations.append("large preset catalog was sampled by bounded stratified pages")
    if exact_names and not exact_matches:
        limitations.append("one or more exact preset preferences could not be resolved from the supplied access surface")
    if current_unresolved:
        limitations.append("current preset name was reported without a verified index and was not added as a candidate")
    if count is None:
        limitations.append("preset count was not reported; coverage cannot be claimed complete")
    if warnings:
        limitations.append("one or more bounded preset pages could not be loaded")
    if omitted:
        limitations.append(f"{omitted} reported preset slots were omitted from the bounded candidate set")
    coverage = PresetCandidateCoverage(
        reported_preset_count=count,
        pages_examined=tuple(page.start for page in pages),
        unique_presets_considered=len(rows),
        coverage_mode=mode,
        exact_preference_matches=exact_matches,
        accepted_history_candidates=accepted_matches,
        anchor_candidates=anchor_matches,
        excluded_candidates=tuple(dict.fromkeys(excluded_matches)),
        seed_derived_candidates=tuple(dict.fromkeys(seed_matches)),
        omitted_count=omitted,
        limitations=tuple(dict.fromkeys(limitations)),
    )
    return PresetCandidateSet(presets=tuple(rows), pages=tuple(pages), coverage=coverage, warnings=warnings)


discover_candidates = discover_preset_candidates
discover_preset_candidate_set = discover_preset_candidates


class PresetCandidateDiscovery:
    """Small state-free façade for callers that hold a policy instance."""

    def __init__(self, policy: PresetCandidateDiscoveryPolicy | Mapping[str, Any] | None = None) -> None:
        self.policy = (
            policy
            if isinstance(policy, PresetCandidateDiscoveryPolicy)
            else PresetCandidateDiscoveryPolicy.model_validate(policy or {})
        )

    def discover(self, catalog: Any = None, **kwargs: Any) -> PresetCandidateSet:
        return discover_preset_candidates(catalog, policy=self.policy, **kwargs)


PresetDiscoveryService = PresetCandidateDiscovery


__all__ = [
    "DiscoveryPagePurpose",
    "MAX_DISCOVERY_CANDIDATES",
    "MAX_DISCOVERY_EXACT_REQUESTS",
    "MAX_DISCOVERY_PAGES",
    "PresetCandidateCoverage",
    "PresetCandidateDiscoveryPolicy",
    "PresetCandidatePage",
    "PresetCandidateSet",
    "PresetCandidateDiscovery",
    "PresetDiscoveryService",
    "discover_candidates",
    "discover_preset_candidate_set",
    "discover_preset_candidates",
]
