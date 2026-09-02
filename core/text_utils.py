"""Text utilities: emotion tags and display-safe cleanup (plan section 13).

Final companion replies start with ``[EMOTION: name]`` on their own line.
All tags are stripped from display text; unknown final emotions normalize to
``neutral``. Asterisk roleplay actions are structural-law violations and are
stripped (plan section 7.3).
"""

from __future__ import annotations

import re

from .constants import DEFAULT_EMOTION, FINAL_EMOTIONS

_EMOTION_TAG_RE = re.compile(r"\[EMOTION:\s*([A-Za-z_]+)\s*\]")
_ASTERISK_RE = re.compile(r"\*[^*\n]*\*")

_FINAL_SET = frozenset(FINAL_EMOTIONS)


def normalize_emotion(name: str | None) -> str:
    """Map an emotion name to the v1 wire palette; unknown -> neutral."""
    if not name:
        return DEFAULT_EMOTION
    lowered = name.strip().lower()
    return lowered if lowered in _FINAL_SET else DEFAULT_EMOTION


def parse_emotion_reply(raw: str) -> tuple[str, str]:
    """Split a raw LLM reply into (display_text, emotion).

    The leading ``[EMOTION: name]`` tag selects the final emotion; every tag
    anywhere in the body is stripped from display text.
    """
    emotion: str | None = None
    for match in _EMOTION_TAG_RE.finditer(raw):
        emotion = match.group(1)
        break
    text = _EMOTION_TAG_RE.sub("", raw)
    text = strip_asterisk_actions(text).strip()
    return text, normalize_emotion(emotion)


def strip_asterisk_actions(text: str) -> str:
    """Remove *roleplay action* spans; they are never emitted (7.3)."""
    cleaned = _ASTERISK_RE.sub("", text)
    # Collapse whitespace runs left behind by removals.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned
