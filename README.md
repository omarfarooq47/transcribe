# Audio Transcription REST API

Asynchronous audio transcription service built with FastAPI, Celery, Redis, SQLite, and OpenAI Whisper.

Upload an audio file, receive a job ID immediately, and poll for status until the transcript is ready. Failed jobs can be retried.

## Features

- `POST /transcriptions` — accept audio uploads and queue transcription (202 Accepted)
- `GET /transcriptions/{job_id}` — check status / retrieve transcript
- `POST /transcriptions/{job_id}/retry` — retry failed jobs
- Background Celery worker with Whisper (`base` model by default)
- Automatic retries for transient failures (exponential backoff: 30s / 60s / 120s)
- Transcripts persisted in SQLite and written to `outputs/{job_id}.json`

## Requirements

- Python 3.11+
- Redis (via Docker Compose or a local install)
- `ffmpeg` (required by Whisper for audio decoding)

  ```bash
  # macOS
  brew install ffmpeg

  # Debian/Ubuntu
  sudo apt-get install ffmpeg
  ```

## Project layout

```text
app/
├── main.py                 # FastAPI application
├── config.py               # Settings (env-overridable)
├── database.py             # SQLAlchemy engine / sessions
├── models.py               # TranscriptionJob ORM
├── schemas.py              # Pydantic response models
├── exceptions.py           # Domain errors
├── celery_app.py           # Celery app + transcription task
├── api/routes.py           # REST endpoints
└── services/
    ├── storage.py          # Upload / output file handling
    ├── transcription.py    # Whisper integration
    └── jobs.py             # Job lifecycle logic
uploads/                    # Stored audio files
outputs/                    # Transcript JSON artifacts
Design.md                   # Architecture and design decisions
```

## Setup

```bash
# 1. Start Redis
docker compose up -d

# 2. Create a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. (Optional) copy env overrides
# export WHISPER_MODEL=base
# export REDIS_URL=redis://localhost:6379/0
# export DATABASE_URL=sqlite:///./transcriptions.db
```

## Run

You need **three** processes: Redis, the API, and a Celery worker.

If jobs stay at `QUEUED`, the worker is almost certainly not running.

```bash
# Terminal 1 — Redis (if not already up)
docker compose up -d

# Terminal 2 — API
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 3 — Celery worker (required for status to leave QUEUED)
source .venv/bin/activate
celery -A app.celery_app.celery worker --loglevel=info --concurrency=1
```

On first start the worker downloads the Whisper model (~140MB for `base`); that can take a minute before transcription begins.

Interactive docs: [http://localhost:8000/docs](http://localhost:8000/docs)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLOAD_DIR` | `uploads` | Where uploaded audio is stored |
| `OUTPUT_DIR` | `outputs` | Where transcript JSON is written |
| `WHISPER_MODEL` | `base` | Whisper model size (`tiny`, `base`, `small`, …) |
| `MAX_FILE_SIZE` | `26214400` | Max upload size in bytes (25 MB) |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker / result backend |
| `DATABASE_URL` | `sqlite:///./transcriptions.db` | SQLAlchemy database URL |
| `MAX_RETRIES` | `3` | Automatic Celery retries for transient errors |
| `ALLOWED_EXTENSIONS` | `mp3,wav,m4a,flac` | Accepted audio extensions |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

Settings are loaded via `pydantic-settings` (environment variables or `.env`).

## API examples

### Upload audio

```bash
curl -X POST http://localhost:8000/transcriptions \
  -F "file=@sample.mp3"
```

Response (`202 Accepted`):

```json
{
  "job_id": "4afab682-3f59-4c8d-a08d-b5fd4d2b8a71",
  "status": "QUEUED"
}
```

### Check status

```bash
curl http://localhost:8000/transcriptions/4afab682-3f59-4c8d-a08d-b5fd4d2b8a71
```

While processing:

```json
{
  "job_id": "4afab682-3f59-4c8d-a08d-b5fd4d2b8a71",
  "status": "PROCESSING"
}
```

When completed:

```json
{
  "job_id": "4afab682-3f59-4c8d-a08d-b5fd4d2b8a71",
  "status": "COMPLETED",
  "transcript": "...",
  "language": "en",
  "segments": [
    {
      "id": 0,
      "start": 0.0,
      "end": 4.2,
      "text": " Hello, this is a sample."
    }
  ]
}
```

When failed:

```json
{
  "job_id": "4afab682-3f59-4c8d-a08d-b5fd4d2b8a71",
  "status": "FAILED",
  "error": "Audio decoding failed."
}
```

### Retry a failed job

```bash
curl -X POST http://localhost:8000/transcriptions/4afab682-3f59-4c8d-a08d-b5fd4d2b8a71/retry
```

```json
{
  "job_id": "4afab682-3f59-4c8d-a08d-b5fd4d2b8a71",
  "status": "QUEUED",
  "message": "Retry scheduled."
}
```

Retrying a completed job returns `409 Conflict`.

### Health check

```bash
curl http://localhost:8000/health
```

## Supported uploads

- Extensions: `.mp3`, `.wav`, `.m4a`, `.flac`
- Max size: 25 MB
- Empty or missing files are rejected with `400`

## Job statuses

| Status | Meaning |
|--------|---------|
| `QUEUED` | Accepted; waiting for a worker |
| `PROCESSING` | Whisper transcription in progress |
| `COMPLETED` | Transcript available |
| `FAILED` | Terminal until a manual retry |

## Retry behavior

- **Automatic:** transient errors only (filesystem blips, unexpected worker faults), up to 3 times with 30s / 60s / 120s backoff
- **Manual:** `POST /transcriptions/{job_id}/retry` for `FAILED` jobs only; reuses the stored audio file

## Design notes

See [Design.md](Design.md) for architecture decisions, module boundaries, and rationale.
