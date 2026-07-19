from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.exceptions import PermanentTranscriptionError, TransientTranscriptionError

logger = logging.getLogger(__name__)

_model = None


def load_whisper_model() -> Any:
    """Load the Whisper model once per process."""
    global _model
    if _model is not None:
        return _model

    import whisper

    settings = get_settings()
    logger.info("Loading Whisper model '%s'...", settings.whisper_model)
    _model = whisper.load_model(settings.whisper_model)
    logger.info("Whisper model '%s' loaded.", settings.whisper_model)
    return _model


def get_model() -> Any:
    if _model is None:
        return load_whisper_model()
    return _model


def transcribe(audio_path: Path) -> dict[str, Any]:
    """
    Run Whisper transcription.

    Returns dict with keys: text, language, segments.
    """
    path = Path(audio_path)
    if not path.is_file():
        raise PermanentTranscriptionError(f"Audio file not found: {path}")
    if path.stat().st_size == 0:
        raise PermanentTranscriptionError("Audio file is empty.")

    try:
        model = get_model()
        result = model.transcribe(str(path))
    except PermanentTranscriptionError:
        raise
    except FileNotFoundError as exc:
        raise PermanentTranscriptionError(str(exc)) from exc
    except (OSError, IOError) as exc:
        # Temporary filesystem issues are retryable.
        raise TransientTranscriptionError(f"Temporary filesystem error: {exc}") from exc
    except Exception as exc:
        message = str(exc).lower()
        permanent_markers = (
            "failed to load audio",
            "invalid data",
            "could not open",
            "unsupported",
            "corrupt",
            "decoding",
            "no such file",
            "format not recognised",
            "format not recognized",
        )
        if any(marker in message for marker in permanent_markers):
            raise PermanentTranscriptionError(f"Audio decoding failed: {exc}") from exc
        # Unexpected errors: treat as transient so Celery can retry a few times.
        raise TransientTranscriptionError(f"Transcription failed: {exc}") from exc

    text = (result.get("text") or "").strip()
    language = result.get("language") or "unknown"
    segments = result.get("segments") or []

    # Normalize segments to JSON-serializable dicts.
    normalized_segments: list[dict[str, Any]] = []
    for segment in segments:
        if isinstance(segment, dict):
            normalized_segments.append(
                {
                    "id": segment.get("id"),
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "text": segment.get("text"),
                }
            )

    return {
        "text": text,
        "language": language,
        "segments": normalized_segments,
    }
