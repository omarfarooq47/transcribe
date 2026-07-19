# Design: Audio Transcription REST API

This document records the architectural and engineering decisions for the transcription service: what we chose, why, and what we deliberately left out.

---

## Goals

- Accept audio uploads and return a job ID immediately (async transcription).
- Track job lifecycle (`QUEUED` → `PROCESSING` → `COMPLETED` / `FAILED`).
- Support manual retry of failed jobs and automatic retry of transient failures.
- Keep the codebase modular and easy to run locally without a distributed-systems footprint.

Non-goals for this version: authentication, multi-tenancy, horizontal scaling playbooks, and a full migration/ops stack.

---

## High-level architecture

```text
Client
  │
  ▼
FastAPI (Uvicorn)  ──►  SQLite (job metadata)
  │
  │ enqueue job_id
  ▼
Redis (Celery broker/result backend)
  │
  ▼
Celery worker  ──►  Whisper (loaded once per process)
  │
  ├── uploads/   (source audio)
  └── outputs/   (transcript JSON)
```

**Why this shape:** Transcription is CPU/GPU-heavy and can take minutes. Blocking the HTTP request would create timeouts and poor UX. A thin API + background worker is the simplest pattern that still demonstrates a production-style async workflow.

---

## Technology choices

| Choice | Decision | Rationale |
|--------|----------|-----------|
| Web framework | FastAPI | Native async support, OpenAPI docs, Pydantic validation, clear DI via `Depends`. |
| ORM | SQLAlchemy | Familiar, portable across SQLite and PostgreSQL if we switch later. |
| Job queue | Celery + Redis | Mature retry/backoff, process isolation from the API, easy local broker via Docker. |
| ASR | Open-source Whisper (`base` by default) | No paid API dependency; model size is configurable for quality vs. speed. |
| Database | SQLite | Zero local setup for a single-node demo; schema is dialect-portable via SQLAlchemy. |
| Config | `pydantic-settings` | Typed settings, env overrides, no scattered hardcoded paths/limits. |
| ASGI server | Uvicorn | Standard FastAPI pairing. |

### Why not process transcription inside FastAPI?

Whisper model load and inference are long-running. Doing them in the request path would:

- Hold HTTP connections open for the full inference duration.
- Block or starve the API under concurrent uploads.
- Force model lifecycle to share the API process (harder restarts, memory pressure).

Celery keeps inference in a dedicated process where the model can stay warm.

### Why Redis instead of a DB-backed queue?

Redis is the default Celery broker: low latency, well-documented retry semantics, and trivial to run with Docker Compose. Job *metadata* still lives in SQLite so status queries do not depend on Celery result TTL or broker state.

### Why SQLite instead of PostgreSQL?

For a local/production-style *sample*, SQLite removes operational friction. The `DATABASE_URL` setting allows pointing at PostgreSQL later without rewriting the domain model. We use `create_all` on startup rather than Alemmic migrations to keep the first version small.

---

## Module boundaries

```text
app/
  api/routes.py          # HTTP only: validate input, call services, map status codes
  services/jobs.py       # Job lifecycle: create, get, retry, status transitions
  services/storage.py    # Filesystem: uploads + output JSON
  services/transcription.py  # Whisper load + transcribe
  celery_app.py          # Worker process, task, autoretry
  models.py / schemas.py # Persistence vs. API contracts
  config.py / database.py
```

**Principles:**

1. **Routes stay thin.** Handlers do not open files for Whisper, touch Celery internals beyond “enqueue,” or embed status-machine rules.
2. **Business logic lives in services.** Retry eligibility, validation, and persistence updates are testable without spinning up HTTP.
3. **Worker owns inference.** Whisper is loaded once per worker process (`worker_process_init` or lazy singleton), never on every task and never inside the API process on each request.
4. **Storage is a separate concern.** Path layout and JSON output format can change without touching route handlers.

---

## Job lifecycle

```text
                 POST /transcriptions
                         │
                         ▼
                      QUEUED
                         │
              Celery picks up task
                         ▼
                    PROCESSING
                    /         \
                   /           \
            success             permanent failure
                 │                    │
                 ▼                    ▼
            COMPLETED               FAILED
                                      │
                         POST .../retry (manual)
                                      │
                                      ▼
                                   QUEUED  (retry_count++)
```

### Status meanings

| Status | Meaning |
|--------|---------|
| `QUEUED` | Accepted; waiting for a worker. |
| `PROCESSING` | Worker has claimed the job and is running Whisper. |
| `COMPLETED` | Transcript (and language) persisted; output JSON written. |
| `FAILED` | Terminal for automatic flow until a client retries. |

### Dual persistence of transcripts

On success we store:

- **Database:** full text + language for fast `GET /transcriptions/{job_id}`.
- **`outputs/{job_id}.json`:** richer artifact (`text`, `language`, `segments`) for offline inspection or later tooling.

The API reads status from the DB; the JSON file is the durable transcription artifact.

---

## Upload and validation

- Multipart field: `file`.
- Allowed extensions: `mp3`, `wav`, `m4a`, `flac`.
- Max size: 25 MB (`MAX_FILE_SIZE`).
- Reject empty files and missing uploads with **400**.

Files are saved as `uploads/{job_id}_{sanitized_original_name}` so:

- Filenames stay unique even if clients upload the same name twice.
- The original basename remains recoverable for debugging.
- Path traversal risk from client filenames is reduced via sanitization.

The API returns **202 Accepted** with `{job_id, status: QUEUED}` and does not wait for Whisper.

---

## Retry strategy

Two distinct retry paths:

### 1. Automatic (worker / Celery)

Used only for **transient** failures, for example:

- Temporary filesystem errors (`OSError` / `IOError`).
- Broker/connectivity blips.
- Unexpected worker crashes that Celery can safely re-run.

**Not** retried automatically:

- Unsupported / corrupt / empty audio (client or data problem).
- Decode failures that will fail the same way on every attempt.

Limits: `MAX_RETRIES = 3` with exponential backoff **30s → 60s → 120s** (`countdown = 30 * 2 ** retry_index`). After exhaustion, the job is marked `FAILED` with an error message.

### 2. Manual (`POST /transcriptions/{job_id}/retry`)

- Allowed only when status is `FAILED`.
- Resets `error_message`, sets status to `QUEUED`, increments `retry_count`, re-enqueues using the **existing** upload (no re-upload).
- `COMPLETED` → **409 Conflict** (idempotent “don’t re-run success”).
- Other non-failed states → conflict/error with a clear message so clients don’t double-queue in-flight work.

`retry_count` on the row tracks **manual** retries for observability; Celery’s own retry counter governs automatic backoff independently.

---

## Error handling and HTTP mapping

| Situation | Status |
|-----------|--------|
| Invalid / empty / oversized / wrong-type file | 400 |
| Unknown `job_id` | 404 |
| Retry on completed (or otherwise non-retryable) job | 409 |
| Unexpected server failure | 500 |

Responses use a simple `{ "detail": "..." }` (or endpoint-specific fields). Internal stack traces are logged, never returned to clients.

Domain exceptions raised from services are translated in FastAPI exception handlers so routes stay free of status-code branching.

---

## Configuration

All tunables live in settings (env-overridable), including:

`UPLOAD_DIR`, `OUTPUT_DIR`, `WHISPER_MODEL`, `MAX_FILE_SIZE`, `REDIS_URL`, `DATABASE_URL`, `MAX_RETRIES`, allowed extensions.

This keeps deployments (local vs. staging) from requiring code edits and makes model size swaps (`tiny` / `base` / `small` / …) a config change.

---

## Logging

Python `logging` (not `print`) records:

- Upload received
- Job queued
- Transcription started / completed
- Retry scheduled (manual and automatic), including retry count
- Failures and exceptions

Enough to reconstruct a job’s path through the system without attaching a full observability stack.

---

## Concurrency and consistency notes

- Job identity is a UUID (`job_id`) exposed to clients; internal numeric `id` is an implementation detail.
- Status updates happen in the worker after claim (`PROCESSING`) and on terminal outcomes. Clients poll `GET`; there is no websocket/push layer in v1.
- SQLite is sufficient for low concurrency. Under heavier write load, move `DATABASE_URL` to PostgreSQL; the service boundaries do not change.
- Whisper is loaded **once per worker process**. Multiple concurrent Celery prefork workers each hold their own model copy (memory tradeoff for parallelism).

---

## Local runtime topology

Three processes:

1. **Redis** — `docker compose` (broker only).
2. **Uvicorn** — FastAPI app.
3. **Celery worker** — transcription tasks.

This mirrors a minimal production split (API vs. worker) without introducing Kubernetes or a message bus beyond Redis.

---

## Explicitly deferred

- AuthN/AuthZ and rate limiting
- Multi-tenant isolation and per-user quotas
- Alembic migrations / managed PostgreSQL from day one
- Streaming progress, webhooks, or WebSocket status
- GPU scheduling, model sharding, or batching across jobs
- Object storage (S3) instead of local `uploads/` / `outputs/`

These can be layered later without abandoning the service boundaries above.

---

## Summary of design intent

Prefer a **clear async job API**, **strict separation of HTTP / domain / worker / storage**, and **honest retry semantics** (transient vs. permanent) over premature distribution. The result is a maintainable local service that still behaves like a production transcription backend: accept fast, process in the background, observe status, and recover from failure deliberately.
