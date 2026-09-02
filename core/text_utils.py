"""Text utilities: emotion segments, display-safe scrubbing, chunking.

Covers plan sections 7.3, 12.23, 13.2 and 13.4:

- Final companion replies start with ``[EMOTION: name]`` on their own line;
  multiple segment tags carry per-segment emotions.
- All tags are stripped from display text and TTS input; unknown final
  emotions normalize to ``neutral``.
- Reasoning blocks, unsupported control tags, and asterisk roleplay actions
  are structural-law violations and are scrubbed.
- ``chunk_segments`` groups segments into TTS chunks preserving sentence
  boundaries and each chunk's active emotion, never emitting empty chunks.
"""

from __future__ import annotations

import re

from .constants import DEFAULT_EMOTION, FINAL_EMOTIONS

_EMOTION_TAG_RE = re.compile(r"\[EMOTION:\s*([A-Za-z_]+)\s*\]")
_ASTERISK_RE = re.compile(r"\*[^*\n]*\*")
# Unsupported control tags such as [STATUS: question] or [TOOL: x]. Uppercase
# ``NAME:`` shape only, so ordinary bracketed prose like [Note: ...] survives.
_CONTROL_TAG_RE = re.compile(r"\[[A-Z][A-Z0-9_]*\s*:\s*[^\]\n]*\]")
_REASONING_PAIR_RE = re.compile(
    r"<(think|thinking|reasoning)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
# An unclosed reasoning block at the very start of a reply swallows the rest;
# nothing displayable follows it.
_REASONING_OPEN_RE = re.compile(
    r"^\s*<(?:think|thinking|reasoning)\b[^>]*>.*", re.IGNORECASE | re.DOTALL
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？…])\s+")

_FINAL_SET = frozenset(FINAL_EMOTIONS)


def normalize_emotion(name: str | None) -> str:
    """Map an emotion name to the v1 wire palette; unknown -> neutral."""
    if not name:
        return DEFAULT_EMOTION
    lowered = name.strip().lower()
    return lowered if lowered in _FINAL_SET else DEFAULT_EMOTION


def strip_reasoning_blocks(text: str) -> str:
    """Remove paired reasoning blocks and a leading unclosed one."""
    cleaned = _REASONING_PAIR_RE.sub("", text)
    cleaned = _REASONING_OPEN_RE.sub("", cleaned, count=1)
    return cleaned


def strip_control_tags(text: str) -> str:
    """Remove unsupported ``[NAME: value]`` control tags (plan 12.23)."""
    return _CONTROL_TAG_RE.sub("", text)


def strip_asterisk_actions(text: str) -> str:
    """Remove *roleplay action* spans; they are never emitted (7.3)."""
    cleaned = _ASTERISK_RE.sub("", text)
    # Collapse whitespace runs left behind by removals.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _scrub(text: str) -> str:
    """Apply every display-safe scrubber to one segment body."""
    text = strip_asterisk_actions(text)
    text = strip_control_tags(text)
    return text.strip()


def parse_emotion_segments(raw: str) -> list[dict]:
    """Parse a reply into display-safe ``{"text", "emotion"}`` segments.

    Text before the first tag becomes a neutral-emotion segment. Every
    ``[EMOTION: name]`` tag starts a new segment and selects its emotion;
    tags are stripped from the text. Empty segments are dropped, so an
    emotion-only reply yields ``[]`` (the caller retries once, plan 7.3).
    """
    cleaned = strip_reasoning_blocks(raw)
    matches = list(_EMOTION_TAG_RE.finditer(cleaned))
    if not matches:
        text = _scrub(cleaned)
        return [{"text": text, "emotion": DEFAULT_EMOTION}] if text else []

    segments: list[dict] = []
    leading = _scrub(cleaned[: matches[0].start()])
    if leading:
        segments.append({"text": leading, "emotion": DEFAULT_EMOTION})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        text = _scrub(cleaned[match.end() : end])
        if text:
            segments.append({"text": text, "emotion": normalize_emotion(match.group(1))})
    return segments


def join_segments(segments: list[dict]) -> str:
    """Display text for a segment list (used by done/history/chat_sync)."""
    return "\n\n".join(str(segment.get("text", "")) for segment in segments)


def parse_emotion_reply(raw: str) -> tuple[str, str]:
    """Backward-compatible ``(display_text, first_emotion)`` view."""
    segments = parse_emotion_segments(raw)
    if not segments:
        return "", DEFAULT_EMOTION
    return join_segments(segments), segments[0]["emotion"]


def split_long_text(text: str, size: int) -> list[str]:
    """Split one oversized segment at sentence boundaries where possible.

    Sentences accumulate up to ``size`` characters; a single sentence longer
    than ``size`` is kept whole rather than shredded mid-sentence.
    """
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= size:
            current = f"{current} {sentence}"
        else:
            pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)
    return pieces


def chunk_segments(segments: list[dict], threshold: int, size: int) -> list[dict]:
    """Group emotion segments into ordered TTS chunks (plan section 13.4).

    - Total length <= ``threshold``: segments pass through unsplit.
    - Otherwise consecutive same-emotion segments pack up to ``size``
      characters; an oversized segment splits at sentence boundaries.
    - Each chunk keeps its active emotion; empty chunks are never produced.
    """
    units = [
        {
            "text": str(segment.get("text", "")).strip(),
            "emotion": segment.get("emotion", DEFAULT_EMOTION),
        }
        for segment in segments
        if str(segment.get("text", "")).strip()
    ]
    if not units:
        return []
    total = sum(len(unit["text"]) for unit in units)
    if threshold > 0 and total <= threshold:
        return units

    size = max(size, 1)
    chunks: list[dict] = []
    current_text = ""
    current_emotion: str | None = None

    def flush() -> None:
        nonlocal current_text
        if current_text.strip():
            chunks.append(
                {"text": current_text.strip(), "emotion": current_emotion or DEFAULT_EMOTION}
            )
        current_text = ""

    for unit in units:
        text = unit["text"]
        if len(text) > size:
            flush()
            for piece in split_long_text(text, size):
                chunks.append({"text": piece, "emotion": unit["emotion"]})
            continue
        if current_emotion is None or current_emotion != unit["emotion"]:
            flush()
            current_emotion = unit["emotion"]
        elif current_text and len(current_text) + 1 + len(text) > size:
            flush()
        current_text = f"{current_text} {text}" if current_text else text
    flush()
    return chunks
