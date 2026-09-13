"""Small, explainable musical starting points for loaded sound selection.

These authored arrangements are suggestions, not genre rules or preset facts.
Explicit roles and product/preset preferences are left intact. The connected
model can express more specific musical reasoning through structured roles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import (
    SoundCreativeDirection,
    SoundMusicalDirection,
    SoundRoleRequest,
    SoundSelectionRequest,
)


@dataclass(frozen=True)
class _Profile:
    profile_id: str
    name: str
    aliases: tuple[str, ...]
    roles: tuple[SoundRoleRequest, ...]
    groove: str
    timbre: str


def _role(role_id: str, role_type: str, *descriptors: str, required: bool = True) -> SoundRoleRequest:
    return SoundRoleRequest(
        role_id=role_id,
        role_type=role_type,
        desired_descriptors=descriptors,
        required=required,
    )


_PROFILES = (
    _Profile(
        "deep_house", "Deep house", ("deep house",),
        (_role("drums", "drums", "dry", "percussive"),
         _role("primary_bass", "bass", "warm", "sub-heavy"),
         _role("main_chords", "chords", "warm", "plucked"),
         _role("texture", "texture", "airy", "wide", required=False)),
        "Start with a steady four-beat kick; let offbeat hats and restrained syncopation supply motion.",
        "Try rounded bass and warm chord stabs, leaving space for the requested vocal or lead.",
    ),
    _Profile(
        "house", "House", ("house",),
        (_role("drums", "drums", "percussive", "bright"),
         _role("primary_bass", "bass", "plucked", "warm"),
         _role("main_chords", "chords", "bright", "plucked"),
         _role("main_lead", "lead", "bright", required=False)),
        "Start with a four-beat kick, backbeat clap/snare and offbeat hats; adapt swing to the brief.",
        "Try a short bass envelope and clear chord attacks before adding wide sustained layers.",
    ),
    _Profile(
        "trap", "Trap", ("trap",),
        (_role("drums", "drums", "dry", "percussive"),
         _role("primary_bass", "bass", "sub-heavy", "mono"),
         _role("main_chords", "chords", "dark", "plucked"),
         _role("main_lead", "lead", "airy", "metallic", required=False)),
        "Start with a half-time backbeat, sparse kick placements and changing hat subdivisions.",
        "Try a sustained sub-capable bass and sparse plucked or bell-like harmony; preserve room for vocals.",
    ),
    _Profile(
        "lofi_hip_hop", "Lo-fi hip-hop", ("lo fi hip hop", "lofi hip hop", "lo fi", "lofi", "boom bap"),
        (_role("drums", "drums", "organic", "dark"),
         _role("primary_bass", "bass", "warm", "mono"),
         _role("main_chords", "chords", "warm", "acoustic"),
         _role("texture", "texture", "retro", "soft", required=False)),
        "Try a relaxed backbeat and light swing; retain intentional gaps rather than filling every subdivision.",
        "Try warm keys or sampled harmony with rounded bass. Texture and degradation remain optional.",
    ),
    _Profile(
        "drum_and_bass", "Drum and bass", ("drum and bass", "drum n bass", "dnb", "d n b"),
        (_role("drums", "drums", "percussive", "bright"),
         _role("primary_bass", "bass", "sub-heavy", "evolving"),
         _role("main_chords", "chords", "wide", "sustained"),
         _role("texture", "texture", "airy", required=False)),
        "Try a fast breakbeat with a clear snare backbeat; use ghost notes and bass rests for syncopation.",
        "Separate the low bass foundation from moving upper texture; choose softer or harsher layers from the brief.",
    ),
    _Profile(
        "ambient", "Ambient", ("ambient",),
        (_role("main_chords", "chords", "sustained", "wide"),
         _role("texture", "texture", "evolving", "airy"),
         _role("primary_bass", "bass", "soft", "sustained", required=False)),
        "Let slow phrase changes and overlapping envelopes carry motion; drums are optional and are not assumed.",
        "Try long, evolving harmonic layers with a restrained low foundation and clear space between textures.",
    ),
    _Profile(
        "synthwave", "Synthwave", ("synthwave", "synth wave", "retrowave"),
        (_role("drums", "drums", "retro", "percussive"),
         _role("primary_bass", "bass", "retro", "plucked"),
         _role("main_chords", "chords", "retro", "wide"),
         _role("main_lead", "lead", "synthetic", "bright", required=False)),
        "Try an even pulsing bass or arpeggio against a clear backbeat; add fills at phrase boundaries.",
        "Try analog-style bass, broad synth chords and a focused lead; keep low notes centered.",
    ),
    _Profile(
        "techno", "Techno", ("techno",),
        (_role("drums", "drums", "percussive", "dry"),
         _role("primary_bass", "bass", "mono", "dark"),
         _role("main_lead", "lead", "synthetic", "evolving"),
         _role("texture", "texture", "metallic", required=False)),
        "Try a steady kick with interlocking percussion and short repeated motifs; evolve the pattern gradually.",
        "Try a centered bass and one evolving synth motif before adding metallic or noisy accents.",
    ),
    _Profile(
        "rnb", "R&B", ("r and b", "rnb", "r n b", "rhythm and blues"),
        (_role("drums", "drums", "soft", "dry"),
         _role("primary_bass", "bass", "warm", "mono"),
         _role("main_chords", "chords", "warm", "soft"),
         _role("texture", "texture", "airy", required=False)),
        "Try a relaxed, syncopated backbeat and leave gaps around vocal phrases.",
        "Try warm keys, round bass and a restrained supporting texture, with the vocal carrying the foreground.",
    ),
    _Profile(
        "pop", "Pop", ("pop",),
        (_role("drums", "drums", "clean", "percussive"),
         _role("primary_bass", "bass", "clean", "warm"),
         _role("main_chords", "chords", "clean", "bright"),
         _role("main_lead", "lead", "bright", required=False)),
        "Start with a readable backbeat and phrase-level contrast; keep the hook rhythm distinct from the accompaniment.",
        "Try clear chord attacks and controlled bass; add a lead only when it complements the requested vocal hook.",
    ),
)
_GENERIC = _Profile(
    "generic", "General arrangement", (),
    (_role("drums", "drums"), _role("primary_bass", "bass"),
     _role("main_chords", "chords"), _role("main_lead", "lead", required=False)),
    "Choose groove and tempo from the user's request; no genre-specific rhythm is inferred.",
    "Start from drums, bass and harmony, adding a lead only when the arrangement needs it.",
)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold().replace("&", " and ")))


def _matches(text: str) -> tuple[_Profile, ...]:
    matches: list[tuple[int, _Profile]] = []
    for profile in _PROFILES:
        for alias in profile.aliases:
            # Avoid assigning a style explicitly ruled out in simple wording.
            expression = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            positions = tuple(re.finditer(expression, text))
            if any(not re.search(r"\b(?:no|not|without|avoid)\s+$", text[:item.start()]) for item in positions):
                matches.append((len(alias), profile))
                break
    matches.sort(key=lambda item: (-item[0], item[1].profile_id))
    # Deep house includes the word house; keep the specific profile only.
    if any(profile.profile_id == "deep_house" for _, profile in matches):
        matches = [(size, profile) for size, profile in matches if profile.profile_id != "house"]
    return tuple(profile for _, profile in matches)


def _brief_role_scope(roles: tuple[SoundRoleRequest, ...], brief: str) -> tuple[SoundRoleRequest, ...]:
    """Honor simple explicit omissions/solo roles before filling defaults."""

    text = _normalize(brief)
    aliases = {
        "drums": r"drums?|percussion",
        "primary_bass": r"bass|sub bass",
        "main_chords": r"chords?|harmony|keys",
        "main_lead": r"lead|melody",
        "texture": r"texture|pad",
    }
    only = {
        role_id for role_id, pattern in aliases.items()
        if re.search(r"\b(?:only|just) (?:a |the )?(?:" + pattern + r")\b|\b(?:" + pattern + r") only\b", text)
    }
    omitted = {
        role_id for role_id, pattern in aliases.items()
        if re.search(r"\b(?:no|without|avoid) (?:any |a |the )?(?:" + pattern + r")\b", text)
    }
    if only:
        defaults = {role.role_id: role for role in (*_GENERIC.roles, _role("texture", "texture"), *roles)}
        return tuple(defaults[role_id].model_copy(update={"required": True}) for role_id in sorted(only - omitted))
    return tuple(role for role in roles if role.role_id not in omitted and (not only or role.role_id in only))


def resolve_musical_direction(request: SoundSelectionRequest) -> SoundMusicalDirection:
    """Resolve editable starting roles without replacing explicit user roles.

    Genre labels have precedence over the free brief. Unknown labels and genre
    blends are reported plainly; no ownership, installed preset or audible
    suitability is inferred from these arrangements.
    """

    structured = request.creative_direction
    genre = structured.genre if isinstance(structured, SoundCreativeDirection) else None
    text = _normalize(genre or " ".join((request.brief, structured if isinstance(structured, str) else "")))
    matches = _matches(text)
    warnings: list[str] = []
    profile = matches[0] if matches else _GENERIC
    if len(matches) > 1:
        warnings.append(
            "Multiple supported genres were requested: " + ", ".join(item.name for item in matches)
            + f". {profile.name} supplies the starting roles; use structured roles to express the blend."
        )
    if not matches:
        warnings.append(
            "No supported genre profile matched; "
            + ("explicit roles are used without genre-specific defaults." if request.roles else "the arrangement uses generic starting roles and makes no genre-specific sound claim.")
        )
    roles = request.roles or _brief_role_scope(profile.roles, request.brief)
    # Existing-palette requests without new roles should retain their existing
    # scope; the palette planner supplies those prior role identities.
    return SoundMusicalDirection(
        profile_id=profile.profile_id,
        profile_name=profile.name,
        matched_from="structured_genre" if genre and matches else "brief" if matches else "generic",
        roles=roles,
        roles_inferred=not bool(request.roles),
        groove_notes=(profile.groove,),
        timbre_notes=(profile.timbre,),
        rationale=(
            f"{profile.name} provides an authored starting point. "
            + ("Explicit roles were preserved exactly. " if request.roles else "Roles were inferred because none were supplied. ")
            + "Product and preset preferences still constrain selection. Groove notes guide composition; sound selection does not write a rhythm."
        ),
        warnings=tuple(warnings),
    )


def supported_musical_profiles() -> tuple[str, ...]:
    return tuple(profile.profile_id for profile in _PROFILES)


__all__ = ["resolve_musical_direction", "supported_musical_profiles"]
