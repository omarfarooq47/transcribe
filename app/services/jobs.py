from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.exceptions import ConflictError, NotFoundError
from app.models import JobStatus, TranscriptionJob
from app.services import storage

logger = logging.getLogger(__name__)


def get_job(db: Session, job_id: str) -> TranscriptionJob:
    job = db.query(TranscriptionJob).filter(TranscriptionJob.job_id == job_id).first()
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found.")
    return job


async def create_transcription_job(db: Session, file: UploadFile) -> TranscriptionJob:
    job_id = str(uuid.uuid4())
    logger.info("Upload received for new job %s (filename=%s)", job_id, file.filename)

    original_name, stored_path = await storage.save_upload(file, job_id)

    job = TranscriptionJob(
        job_id=job_id,
        filename=original_name,
        stored_path=str(stored_path),
        status=JobStatus.QUEUED,
        retry_count=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    _enqueue_transcription(job_id)
    logger.info("Job queued: %s", job_id)
    return job


def schedule_retry(db: Session, job_id: str) -> TranscriptionJob:
    job = get_job(db, job_id)

    if job.status == JobStatus.COMPLETED:
        raise ConflictError("Cannot retry a completed transcription job.")
    if job.status != JobStatus.FAILED:
        raise ConflictError(
            f"Cannot retry job in status '{job.status.value}'. Only FAILED jobs can be retried."
        )

    if not Path(job.stored_path).is_file():
        raise ConflictError("Original audio file is missing; cannot retry.")

    job.status = JobStatus.QUEUED
    job.error_message = None
    job.retry_count += 1
    db.commit()
    db.refresh(job)

    _enqueue_transcription(job_id)
    logger.info(
        "Retry scheduled for job %s (manual retry_count=%d)",
        job_id,
        job.retry_count,
    )
    return job


def mark_processing(db: Session, job_id: str) -> TranscriptionJob:
    job = get_job(db, job_id)
    job.status = JobStatus.PROCESSING
    job.error_message = None
    db.commit()
    db.refresh(job)
    logger.info("Transcription started for job %s", job_id)
    return job


def mark_completed(
    db: Session,
    job_id: str,
    transcript: str,
    language: str,
    segments: list[dict[str, Any]] | None = None,
) -> TranscriptionJob:
    job = get_job(db, job_id)
    job.status = JobStatus.COMPLETED
    job.transcript = transcript
    job.language = language
    job.segments = segments or []
    job.error_message = None
    db.commit()
    db.refresh(job)
    logger.info("Transcription completed for job %s (language=%s)", job_id, language)
    return job


def get_segments(job: TranscriptionJob) -> list[dict[str, Any]]:
    if job.segments:
        return job.segments
    # Fallback for jobs completed before segments were persisted in the DB.
    output = storage.read_transcript_output(job.job_id)
    if output and isinstance(output.get("segments"), list):
        return output["segments"]
    return []


def mark_failed(db: Session, job_id: str, error_message: str) -> TranscriptionJob:
    job = get_job(db, job_id)
    job.status = JobStatus.FAILED
    job.error_message = error_message
    db.commit()
    db.refresh(job)
    logger.error("Transcription failed for job %s: %s", job_id, error_message)
    return job


def _enqueue_transcription(job_id: str) -> None:
    # Late import avoids circular dependency between jobs service and celery_app.
    from app.celery_app import transcribe_audio

    transcribe_audio.delay(job_id)
