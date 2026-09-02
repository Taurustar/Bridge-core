"""Emotion manifest loading and validation (plan section 13.3).

``constants.py`` owns the allowed wire names; the bundled neutral manifest
(``core/emotions.json``) ships schema-complete with no asset URLs. The
manifest may not introduce unknown names. ``EMOTIONS_FILE`` overrides the
bundled path for deployment-local sprite/animation/TTS settings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .constants import FINAL_EMOTIONS, STATUS_EMOTIONS

log = logging.getLogger("bridge.emotions")

BUNDLED_EMOTIONS_FILE = Path(__file__).resolve().parent / "emotions.json"

_REQUIRED_ENTRY_KEYS = {"name", "tts_speed", "sprite_key", "animation_key"}


class EmotionsManifestError(RuntimeError):
    """Raised when the emotions manifest is missing, malformed, or invalid."""


def load_emotions_manifest(path: str | None = None) -> dict:
    """Load and validate the manifest; returns the parsed JSON dict."""
    manifest_path = Path(path) if path else BUNDLED_EMOTIONS_FILE
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EmotionsManifestError(
            f"Emotions manifest not found: {manifest_path}"
        ) from None
    except json.JSONDecodeError as exc:
        raise EmotionsManifestError(
            f"Emotions manifest is not valid JSON: {manifest_path}: {exc}"
        ) from None
    validate_emotions_manifest(data, source=str(manifest_path))
    return data


def validate_emotions_manifest(data: object, source: str = "<manifest>") -> None:
    if not isinstance(data, dict):
        raise EmotionsManifestError(f"{source}: manifest must be a JSON object")
    if not isinstance(data.get("version"), int):
        raise EmotionsManifestError(f"{source}: missing integer 'version'")

    emotions = data.get("emotions")
    if not isinstance(emotions, list) or not emotions:
        raise EmotionsManifestError(f"{source}: 'emotions' must be a non-empty list")

    final_names = set(FINAL_EMOTIONS)
    seen: set[str] = set()
    for entry in emotions:
        if not isinstance(entry, dict) or not _REQUIRED_ENTRY_KEYS <= set(entry):
            raise EmotionsManifestError(
                f"{source}: each emotion entry requires {sorted(_REQUIRED_ENTRY_KEYS)}"
            )
        name = entry["name"]
        if name not in final_names:
            raise EmotionsManifestError(
                f"{source}: manifest may not introduce unknown emotion {name!r}"
            )
        if name in seen:
            raise EmotionsManifestError(f"{source}: duplicate emotion {name!r}")
        seen.add(name)

    status = data.get("status_emotions")
    if not isinstance(status, list):
        raise EmotionsManifestError(f"{source}: 'status_emotions' must be a list")
    status_names = set(STATUS_EMOTIONS)
    for name in status:
        if name not in status_names:
            raise EmotionsManifestError(
                f"{source}: manifest may not introduce unknown status emotion {name!r}"
            )
