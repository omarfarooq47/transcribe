# Design notes

This is a short write-up of how the transcription service is put together and why it looks the way it does. It’s not a product brief — more of a trail of decisions for whoever picks the code up next.

## What we’re solving

People upload audio. We turn it into text. That’s the whole product surface.

The awkward part is that Whisper is slow and hungry. You can’t honestly do that work inside an HTTP request and call it production-shaped. So the API’s job is to accept a file, give you a job id, and get out of the way. Something else does the actual transcription. You poll until it’s done (or failed), and if it failed for a flaky reason you can kick it again.

We also wanted something you can run on a laptop with Redis in Docker — not a mini Kubernetes cluster pretending to be a take-home.

## The shape of the system

There are three processes on purpose:

1. **Uvicorn / FastAPI** — takes uploads, answers status questions, schedules retries.
2. **Redis** — the mailbox Celery uses.
3. **A Celery worker** — loads Whisper once, pulls jobs, writes results.

```text
Client → FastAPI → SQLite (job row)
                 → Redis → Celery worker → Whisper
                              ↓
                    uploads/ + outputs/
```

If only the API is running, jobs sit on `QUEUED` forever. That’s not a bug in the status machine — nothing is consuming the queue. Easy to forget locally; painful once.

## Why not just transcribe in the request?

We tried that mental model and threw it out. Holding a connection open for a multi-minute model run is a bad client experience, a bad ops experience, and a bad way to share memory with the web process. Celery gives us a clean process boundary: the API stays light, the worker can keep the model warm, and a crash in inference doesn’t take the HTTP server with it.

## Stack choices (the boring but useful ones)

**FastAPI** because the validation story with Pydantic is good, the docs come for free, and dependency injection keeps route handlers from growing into god functions.

**Celery + Redis** because retry with backoff is a solved problem there, and Redis is one `docker compose up` away. We could have used a database queue or RQ; Celery is the one most people already know how to operate.

**Open-source Whisper** so we’re not wired to a paid speech API. Default model is `base` — good enough for demos, swappable via config when you want quality or speed instead.

**SQLite** for job metadata. For a single-machine setup it’s boring in the good way: no DB container, no credentials, file on disk. SQLAlchemy means we can point `DATABASE_URL` at Postgres later without redrawing the domain model. We skipped Alembic for now and use `create_all` plus a tiny SQLite column check when we add fields. That’s a trade-off, not a virtue — fine at this scale, replace when the schema churns.

**Config in pydantic-settings** so paths, model name, file size limits, and Redis URL aren’t sprinkled through the code as magic strings.

## How the code is split

Routes are deliberately dumb. They parse the request, call a service, return a response. If you find Whisper imports or status-transition rules in `api/routes.py`, something went wrong.

The interesting bits live under `services/`:

- `jobs.py` — create a job, look it up, decide whether a retry is legal, flip statuses.
- `storage.py` — save the upload, write the output JSON, read it back if needed.
- `transcription.py` — wrap Whisper. Model load happens once per worker process, not once per file.

`celery_app.py` is the worker’s front door: claim job → process → succeed/fail/retry.

That split is what keeps the HTTP layer testable and the worker replaceable. If we ever swapped Celery for something else, the job lifecycle logic shouldn’t have to move.

## Job lifecycle

Upload lands → row created as `QUEUED` → task pushed to Redis → worker sets `PROCESSING` → either `COMPLETED` or `FAILED`.

Completed means we have the transcript text, detected language, and timed segments. Failed means we stopped and stored an error message a human can read. Manual retry is only for `FAILED`; trying to retry a completed job gets a 409, which is the API saying “that already worked, leave it alone.” Same idea for jobs still queued or processing — don’t stack another run on top.

Clients never wait on Whisper in the POST. They get `202` and a job id, then poll `GET /transcriptions/{job_id}`. No websockets, no webhooks in this version. Polling is enough and keeps the surface small.

## Where the transcript lives

We keep two copies of the result on purpose.

The **database** is what the API reads when you ask for status: text, language, segments with start/end timestamps. Fast path for the common case.

The **`outputs/{job_id}.json` file** is the fuller artifact — same text and language, plus the segment list in a form that’s easy to open in an editor or feed into another tool. If an older row somehow lacks segments in the DB, we fall back to that file so the API still returns timestamps.

Uploads themselves sit under `uploads/` as `{job_id}_{safe_original_name}`. Prefixing with the job id avoids collisions when two people upload `interview.mp3`. We sanitize the original name so path tricks don’t sneak through.

## Retries: automatic vs manual

These are not the same thing, and mixing them up causes weird support tickets.

**Automatic retries** are for flaky infrastructure: disk hiccups, weird transient errors, the kind of failure that might succeed if you wait a bit. Celery backs off 30s → 60s → 120s, up to three tries, then we mark the job failed. We do *not* auto-retry corrupt audio, empty files, or decode errors — those will fail the same way every time, and burning retries on them just delays the bad news.

**Manual retry** is the client saying “try this failed job again” via the API. We reuse the file already on disk (no re-upload), clear the error, bump `retry_count`, and enqueue again. That counter is for us humans looking at the row; Celery keeps its own retry count for the automatic path.

## Errors

Invalid file → 400. Unknown job → 404. Retry that doesn’t make sense → 409. Surprises → 500 with a generic message. Stack traces stay in the logs. Domain errors are raised from services and mapped in one place in `main.py`, so routes don’t sprout `if/elif` status code trees.

## Running it day to day

You need Redis, the API, and the worker. Prefer `--concurrency=1` on the worker locally — Whisper is large, and Celery’s default prefork happily starts a pile of children that each try to load the model. One process is plenty until you have a reason to scale.

First worker boot may download the model. That’s a one-time wait, not a stuck queue… unless you forgot to start the worker, in which case it *is* a stuck queue.

## What we left on the table

No auth, no multi-tenant walls, no S3, no Alembic, no GPU scheduling, no push notifications. Those are real needs for a hosted product; they aren’t required to prove the async transcription pattern. The boundaries above are meant to leave room for them without a rewrite.

## In one sentence

Accept fast, transcribe elsewhere, store enough to answer status honestly, and only retry the failures that deserve another shot.
