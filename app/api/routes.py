from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import JobStatus
from app.schemas import JobAcceptedResponse, JobStatusResponse, RetryResponse
from app.services import jobs as jobs_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["transcriptions"])


@router.post(
    "/transcriptions",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_transcription(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> JobAcceptedResponse:
    job = await jobs_service.create_transcription_job(db, file)
    return JobAcceptedResponse(job_id=job.job_id, status=job.status)


@router.get(
    "/transcriptions/{job_id}",
    response_model=JobStatusResponse,
    response_model_exclude_none=True,
)
def get_transcription(job_id: str, db: Session = Depends(get_db)) -> JobStatusResponse:
    job = jobs_service.get_job(db, job_id)

    if job.status == JobStatus.COMPLETED:
        return JobStatusResponse(
            job_id=job.job_id,
            status=job.status,
            transcript=job.transcript,
            language=job.language,
        )
    if job.status == JobStatus.FAILED:
        return JobStatusResponse(
            job_id=job.job_id,
            status=job.status,
            error=job.error_message or "Transcription failed.",
        )
    return JobStatusResponse(job_id=job.job_id, status=job.status)


@router.post(
    "/transcriptions/{job_id}/retry",
    response_model=RetryResponse,
)
def retry_transcription(job_id: str, db: Session = Depends(get_db)) -> RetryResponse:
    job = jobs_service.schedule_retry(db, job_id)
    return RetryResponse(
        job_id=job.job_id,
        status=job.status,
        message="Retry scheduled.",
    )
