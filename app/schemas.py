from pydantic import BaseModel, Field

from app.models import JobStatus


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    transcript: str | None = None
    language: str | None = None
    error: str | None = None


class RetryResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str = Field(default="Retry scheduled.")


class ErrorResponse(BaseModel):
    detail: str
