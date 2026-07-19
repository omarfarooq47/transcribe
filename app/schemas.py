from pydantic import BaseModel, Field

from app.models import JobStatus


class TranscriptSegment(BaseModel):
    id: int | None = None
    start: float
    end: float
    text: str


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    transcript: str | None = None
    language: str | None = None
    segments: list[TranscriptSegment] | None = None
    error: str | None = None


class RetryResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str = Field(default="Retry scheduled.")


class ErrorResponse(BaseModel):
    detail: str
