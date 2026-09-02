"""Owner-authored static lines for no-LLM speech paths (plan sections 7.1, 14.2).

``STATIC_LINES_FILE`` is a human-authored JSON file with EN/ES/JA tables for
``busy``, ``unavailable``, ``soft_block``, and ``stt_empty``. The bundled
``core/static_lines.json`` ships schema-complete with empty values: a blank
line is deliberate silence and produces protocol-only status/done metadata.
The engine never invents static character voice, and there is no
cross-language fallback — if the pinned language's line is blank, no line is
shown.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("bridge.static_lines")

BUNDLED_STATIC_LINES_FILE = Path(__file__).resolve().parent / "static_lines.json"

LINE_KEYS: tuple[str, ...] = ("busy", "unavailable", "soft_block", "stt_empty")
SUPPORTED_LINE_LANGUAGES: tuple[str, ...] = ("en", "es", "ja")


class StaticLinesError(RuntimeError):
    """The static-lines file is missing, malformed, or invalid."""


def load_static_lines(path: str | None = None) -> dict:
    """Load and validate the static-lines table; returns the parsed dict."""
    lines_path = Path(path) if path else BUNDLED_STATIC_LINES_FILE
    try:
        data = json.loads(lines_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StaticLinesError(f"Static lines file not found: {lines_path}") from None
    except json.JSONDecodeError as exc:
        raise StaticLinesError(
            f"Static lines file is not valid JSON: {lines_path}: {exc}"
        ) from None
    validate_static_lines(data, source=str(lines_path))
    return data


def validate_static_lines(data: object, source: str = "<static lines>") -> None:
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        raise StaticLinesError(f"{source}: requires an integer 'version'")
    for language in SUPPORTED_LINE_LANGUAGES:
        table = data.get(language)
        if not isinstance(table, dict):
            raise StaticLinesError(
                f"{source}: missing required language table {language!r}"
            )
        _validate_table(table, language, source)
    for language, table in data.items():
        if language == "version" or language in SUPPORTED_LINE_LANGUAGES:
            continue
        if not isinstance(table, dict):
            raise StaticLinesError(f"{source}: language {language!r} must be an object")
        _validate_table(table, language, source)


def _validate_table(table: dict, language: str, source: str) -> None:
    for key, value in table.items():
        if key not in LINE_KEYS:
            raise StaticLinesError(
                f"{source}: unknown line key {key!r} in {language!r} "
                f"(allowed: {', '.join(LINE_KEYS)})"
            )
        if not isinstance(value, str):
            raise StaticLinesError(
                f"{source}: {language}.{key} must be a string (blank = silence)"
            )


def get_static_line(lines: dict, key: str, language: str) -> str | None:
    """The authored line for ``key`` in ``language``, or None when blank.

    No cross-language fallback: a blank value in the pinned language means
    deliberate silence (protocol-only metadata, plan section 7.1).
    """
    lang = (language or "").strip().lower()
    table = lines.get(lang)
    if not isinstance(table, dict):
        return None
    line = table.get(key)
    if isinstance(line, str) and line.strip():
        return line.strip()
    return None
