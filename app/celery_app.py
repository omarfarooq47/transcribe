from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_process_init
from celery.exceptions import MaxRetriesExceededError

from app.config import get_settings
from app.database import SessionLocal
from app.exceptions import NotFoundError, PermanentTranscriptionError, TransientTranscriptionError
from app.services import jobs as jobs_service
from app.services import storage, transcription

logger = logging.getLogger(__name__)
settings = get_settings()

celery = Celery(
    "transcription",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    broker_connection_retry_on_startup=True,
    # Prefer a single process locally: Whisper is heavy and prefork multiplies RAM.
    worker_concurrency=1,
)


@worker_process_init.connect
def _load_model_on_worker_start(**_kwargs: object) -> None:
    storage.ensure_directories()
    transcription.load_whisper_model()


def _backoff_countdown(retries: int) -> int:
    # 30s, 60s, 120s for retries 0, 1, 2
    return 30 * (2**retries)


@celery.task(
    bind=True,
    name="app.celery_app.transcribe_audio",
    max_retries=settings.max_retries,
    acks_late=True,
)
def transcribe_audio(self, job_id: str) -> dict[str, str]:
    db = SessionLocal()
    try:
        try:
            job = jobs_service.mark_processing(db, job_id)
        except NotFoundError:
            logger.error("Celery task received unknown job_id=%s", job_id)
            return {"job_id": job_id, "status": "NOT_FOUND"}

        try:
            audio_path = storage.resolve_upload_path(job.stored_path)
        except FileNotFoundError as exc:
            jobs_service.mark_failed(db, job_id, str(exc))
            return {"job_id": job_id, "status": "FAILED"}

        try:
            result = transcription.transcribe(audio_path)
        except PermanentTranscriptionError as exc:
            jobs_service.mark_failed(db, job_id, str(exc))
            return {"job_id": job_id, "status": "FAILED"}
        except TransientTranscriptionError as exc:
            logger.warning(
                "Transient failure for job %s (attempt %s/%s): %s",
                job_id,
                self.request.retries + 1,
                settings.max_retries,
                exc,
            )
            try:
                raise self.retry(
                    exc=exc,
                    countdown=_backoff_countdown(self.request.retries),
                )
            except MaxRetriesExceededError:
                jobs_service.mark_failed(
                    db,
                    job_id,
                    f"Transcription failed after {settings.max_retries} retries: {exc}",
                )
                return {"job_id": job_id, "status": "FAILED"}
        except (OSError, IOError) as exc:
            logger.warning("Filesystem error for job %s: %s", job_id, exc)
            try:
                raise self.retry(
                    exc=exc,
                    countdown=_backoff_countdown(self.request.retries),
                )
            except MaxRetriesExceededError:
                jobs_service.mark_failed(
                    db,
                    job_id,
                    f"Transcription failed after {settings.max_retries} retries: {exc}",
                )
                return {"job_id": job_id, "status": "FAILED"}
        except Exception as exc:
            logger.exception("Unexpected error for job %s", job_id)
            try:
                raise self.retry(
                    exc=exc,
                    countdown=_backoff_countdown(self.request.retries),
                )
            except MaxRetriesExceededError:
                jobs_service.mark_failed(
                    db,
                    job_id,
                    f"Unexpected error after {settings.max_retries} retries: {exc}",
                )
                return {"job_id": job_id, "status": "FAILED"}

        storage.write_transcript_output(
            job_id=job_id,
            language=result["language"],
            text=result["text"],
            segments=result["segments"],
        )
        jobs_service.mark_completed(
            db,
            job_id,
            transcript=result["text"],
            language=result["language"],
            segments=result["segments"],
        )
        return {"job_id": job_id, "status": "COMPLETED"}
    finally:
        db.close()
