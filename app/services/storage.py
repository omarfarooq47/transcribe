from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app.config import Settings, get_settings
from app.exceptions import ValidationError

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^\w.\-]+")


def ensure_directories(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)


def sanitize_filename(filename: str) -> str:
    name = Path(filename).name
    sanitized = _UNSAFE_CHARS.sub("_", name).strip("._")
    return sanitized or "audio"


def get_extension(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def validate_upload(file: UploadFile, settings: Settings | None = None) -> str:
    settings = settings or get_settings()

    if file is None or not file.filename:
        raise ValidationError("No file provided.")

    extension = get_extension(file.filename)
    if extension not in settings.allowed_extension_set:
        allowed = ", ".join(sorted(settings.allowed_extension_set))
        raise ValidationError(f"Unsupported file type '.{extension}'. Allowed: {allowed}.")

    return extension


async def save_upload(
    file: UploadFile,
    job_id: str,
    settings: Settings | None = None,
) -> tuple[str, Path]:
    """Validate and persist an uploaded audio file. Returns (original_filename, stored_path)."""
    settings = settings or get_settings()
    ensure_directories(settings)
    validate_upload(file, settings)

    original_name = Path(file.filename or "audio").name
    safe_name = sanitize_filename(original_name)
    stored_name = f"{job_id}_{safe_name}"
    destination = settings.upload_dir / stored_name

    size = 0
    chunk_size = 1024 * 1024

    try:
        with destination.open("wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_file_size:
                    raise ValidationError(
                        f"File exceeds maximum size of {settings.max_file_size} bytes."
                    )
                out.write(chunk)
    except ValidationError:
        if destination.exists():
            destination.unlink(missing_ok=True)
        raise
    except Exception:
        if destination.exists():
            destination.unlink(missing_ok=True)
        raise

    if size == 0:
        destination.unlink(missing_ok=True)
        raise ValidationError("Uploaded file is empty.")

    logger.info("Saved upload for job %s (%d bytes) -> %s", job_id, size, destination)
    return original_name, destination


def write_transcript_output(
    job_id: str,
    language: str,
    text: str,
    segments: list[dict[str, Any]],
    settings: Settings | None = None,
) -> Path:
    settings = settings or get_settings()
    ensure_directories(settings)
    path = settings.output_dir / f"{job_id}.json"
    payload = {
        "job_id": job_id,
        "language": language,
        "text": text,
        "segments": segments,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote transcript output for job %s -> %s", job_id, path)
    return path


def resolve_upload_path(stored_path: str) -> Path:
    path = Path(stored_path)
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {stored_path}")
    return path


def read_transcript_output(
    job_id: str,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    path = settings.output_dir / f"{job_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
