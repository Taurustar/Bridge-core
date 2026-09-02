"""State expression: need zones -> bounded prompt block (plan section 15.7).

``state_expression.py`` converts need zones into a ``[CHARACTER STATE]``
prompt block using the authored ``STATE.md`` sections (``## energy:low`` etc).
No dialogue is scripted by engine code, no numeric values are ever emitted,
and sections missing from the authored file are skipped silently.
"""

from __future__ import annotations

import re

SECTION_RE = re.compile(r"^##\s+(?P<stat>[a-z_]+):(?P<zone>[a-z]+)\s*$", re.MULTILINE)

_ZONE_LABELS = {
    "energy": "energy",
    "hunger": "hunger",
    "stress": "stress",
    "social_battery": "social battery",
    "fun": "fun",
    "hurt": "hurt",
    "bond": "bond",
}


def available_sections(state_md_text: str) -> set[tuple[str, str]]:
    """The ``(stat, zone)`` pairs the authored STATE.md actually defines."""
    if not state_md_text:
        return set()
    return {
        (match.group("stat"), match.group("zone")) for match in SECTION_RE.finditer(state_md_text)
    }


def build_state_block(zones: dict[str, str], state_md_text: str) -> str:
    """The bounded block of plan section 15.7. Empty when nothing applies."""
    known = available_sections(state_md_text)
    lines: list[str] = []
    for stat in _ZONE_LABELS:
        zone = zones.get(stat)
        if not zone:
            continue
        if stat == "bond" and zone == "fine":
            zone = "secure"
        if (stat, zone) not in known:
            continue
        lines.append(f"{_ZONE_LABELS[stat]}: {zone}")
    if not lines:
        return ""
    return "\n".join(
        [
            "[CHARACTER STATE]",
            *lines,
            "",
            "[AGENCY THIS TURN]",
            "- Use the authored STATE.md expression rules for these zones.",
            "- Do not mention numeric values or internal systems.",
        ]
    )
