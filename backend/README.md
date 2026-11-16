# Geetha backend

The Django and Django REST Framework backend for Geetha, an application that turns a text prompt, or an image plus an optional prompt, into a short video. The backend:

- validates generation requests;
- stores jobs and uploaded images;
- runs a background worker that sends each job to an AI client and stores the resulting MP4;
- serves job status, history and videos through a JSON API under `/api/v1/`.

> **Current status.** Generation runs in **mock mode** by default. The mock client renders an FFmpeg **test pattern**, not an AI-generated video. A client for a remote Wan 2.1 service exists, but no such service is part of this repository, and real AI generation has not been integrated or run. See [Mock and real AI generation](#mock-and-real-ai-generation).

Detailed references: [API contracts](../docs/api-documentation.md), [architecture](../docs/architecture.md), [security](../docs/security.md).

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Installation](#installation)
- [Running locally](#running-locally)
- [Generation worker](#generation-worker)
- [Running the tests safely](#running-the-tests-safely)
- [API endpoints](#api-endpoints)
- [CSRF, ownership, rate limits and errors](#csrf-ownership-rate-limits-and-errors)
- [Generation requests](#generation-requests)
- [Upload and size limits](#upload-and-size-limits)
- [Mock and real AI generation](#mock-and-real-ai-generation)
- [Worker reliability](#worker-reliability)
- [Media audit (orphaned files)](#media-audit-orphaned-files)
- [Troubleshooting](#troubleshooting)
- [Production deployment](#production-deployment)

## Architecture

```
backend/
  manage.py
  config/            settings.py (all configuration), urls.py, wsgi.py, asgi.py
  apps/generator/    the one Django app
    models.py        GenerationJob: status, settings, input image, output video, errors
    migrations/      0001_initial.py, the only migration
    urls.py          API routes         views.py        API views
    serializers.py   request validation and response shapes
    validators.py    image checks       profiles.py     accepted model settings
    permissions.py   CSRF for writes    throttles.py    generation rate limits
    active_jobs.py   one active job per owner (MySQL named lock)
    pagination.py    history page size  ranges.py       video byte ranges
    middleware.py    request IDs, request-size limit, trailing-slash 404s
    exceptions.py    the JSON error format
    filenames.py     server-side upload and download names
    media_files.py   safe media paths and file writes
    media_audit.py   the read-only orphaned-media report
    admin.py         read-only job admin
    worker.py        background job processing
    management/commands/
      run_generation_worker.py   a separate worker process
      expire_stale_jobs.py       recovery cycle without a worker
      find_orphaned_media.py     the media audit (report only)
  tests/             the test suite (Django's test runner, no extra packages)
../ai_service/client/  AI clients used by the worker: mock.py (FFmpeg) and remote.py (HTTP)
../frontend/           static HTML/CSS/JS, served by Django in development
../media/              uploads (inputs/) and generated videos (outputs/)
```

How a request flows:

1. **Create.** The browser posts a generation request. The backend checks, in this order, the declared request size, the CSRF token and the rate limit, then validates the request. It then stores a job with status `queued` and returns `202 Accepted`. The request never waits for the video.
2. **Generate.** A worker claims the oldest queued job, sets it to `processing` and calls the AI client. It stores the verified MP4 and sets the job to `completed`, or to `failed` with an error code.
3. **Read.** The browser polls the job's status URL and then plays or downloads the video through the API. Files are never served by path.

Job states are `queued`, `processing`, `completed` and `failed`. `completed` and `failed` are final. A queued job can be cancelled by its owner, and fails by itself after the queue timeout; both end as `failed` (codes `cancelled` and `queue_timeout`).

## Prerequisites

- **Python 3.13.** The only version this project has been tested with (3.13.3). `requirements.txt` pins Django 5.2.18.
- **MySQL 8.0** (tested with 8.0.45). The backend uses MySQL only (`django.db.backends.mysql`); there is no SQLite fallback. Tables use `utf8mb4`, which emoji prompts need.
  - Running the server needs an account with access to the application database.
  - Running the tests needs an account that can also **create and drop** a separate test database. See [Running the tests safely](#running-the-tests-safely).
- **FFmpeg, for mock mode only.** Either `ffmpeg` on `PATH`, or the binary bundled with the `imageio-ffmpeg` package from `requirements.txt`.

## Configuration

Settings are read from environment variables. `config/settings.py` also loads the project-root `.env` file if it exists; a variable already set in the environment, even to an empty value, takes priority over `.env`. Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DEBUG` | `False` | `True`, `1` or `yes` enables debug mode. Use it for local development only. |
| `DJANGO_SECRET_KEY` | none | Required whenever `DJANGO_DEBUG` is off (see below). |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated host names the server answers to. |
| `DJANGO_BEHIND_HTTPS_PROXY` | `False` | `True` only behind an HTTPS reverse proxy that overwrites `X-Forwarded-Proto` on every request. See [Production deployment](#production-deployment). |
| `MYSQL_DATABASE` | `ai_video_generator` | Application database name. |
| `MYSQL_USER`, `MYSQL_PASSWORD` | empty | MySQL credentials. |
| `MYSQL_HOST`, `MYSQL_PORT` | `127.0.0.1`, `3306` | MySQL server. |
| `MEDIA_ROOT`, `MEDIA_URL` | `media`, `/media/` | Upload/video folder, relative to the project root. |
| `AI_SERVICE_MODE` | `mock` | `mock` or `remote` (case-insensitive; blank means `mock`). Any other value stops startup with `ImproperlyConfigured`, so a typo never falls back to mock videos silently. |
| `AI_SERVICE_BASE_URL`, `AI_SERVICE_API_KEY` | empty | Remote AI service address and key. Required only in `remote` mode. |
| `AI_SERVICE_TIMEOUT`, `AI_SERVICE_POLL_INTERVAL`, `AI_SERVICE_MAX_WAIT` | `30`, `10`, `1800` (seconds) | Remote client: timeout of one HTTP request (connect or one read), status polling interval, and the overall deadline for one generation (submission, polling and download together). Whole numbers of at least 1; anything else stops startup. |
| `AI_SERVICE_MAX_VIDEO_BYTES` | `104857600` (100 MiB) | Largest video accepted from the remote service. Larger downloads stop early and fail with `output_too_large`. |
| `RUN_WORKER_IN_PROCESS` | `True` | Start a worker thread inside `runserver`. |
| `GENERATION_QUEUE_TIMEOUT_SECONDS` | `1800` | A job still `queued` this long after creation fails with `queue_timeout`. A whole number of at least 1; anything else stops startup. In remote mode, keep it well above `AI_SERVICE_MAX_WAIT + AI_SERVICE_TIMEOUT`, because a queued job can expire while it waits behind a long generation (the defaults do not; see [Worker reliability](#worker-reliability)). |

**Secret key rules** (`resolve_secret_key` in `config/settings.py`):

- **`DJANGO_DEBUG` off (the default).** `DJANGO_SECRET_KEY` must be set to a real value. Django refuses to start with an `ImproperlyConfigured` error if the key is:
  - missing or blank;
  - `development-only-key-replace-before-deployment`, the built-in development key;
  - `replace-with-a-local-secret`, the placeholder in `.env.example`.

  Surrounding spaces don't get a placeholder past this check. The error names the setting but never shows its value.
- **`DJANGO_DEBUG` on.** A missing or blank key falls back to the development key, and both placeholders are accepted, so local development works without a real key. Never use debug mode for deployment.
- **Exact value.** The key is used exactly as configured; it is never trimmed or logged.

A suitable key can be generated with:

```
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

## Installation

Run from the project root (the folder that contains `requirements.txt`, `backend/` and `frontend/`).

**1. Virtual environment and packages.**

```
python -m venv .venv
.venv\Scripts\Activate.ps1         # Windows PowerShell
.venv\Scripts\activate.bat         # Windows Command Prompt
source .venv/bin/activate          # macOS / Linux
pip install -r requirements.txt
```

If PowerShell refuses to run `Activate.ps1`, skip activation and call the environment's Python directly, for example `.venv\Scripts\python -m pip install -r requirements.txt`, and later `..\.venv\Scripts\python manage.py runserver` from `backend/`.

`requirements.txt` pins Django 5.2.18, Django REST Framework 3.18.3, mysqlclient 2.3.0, python-dotenv, Django's own dependencies, and the optional `imageio-ffmpeg`. No extra packages are needed for the tests.

**2. Environment file.** Copy the example, then edit the copy:

```
Copy-Item .env.example .env        # PowerShell (Command Prompt: copy; macOS / Linux: cp)
```

- Set `MYSQL_USER` and `MYSQL_PASSWORD` to the account from step 3.
- For local development, set `DJANGO_DEBUG=True`, or set a real `DJANGO_SECRET_KEY` (see [Configuration](#configuration)). The example as shipped (`DJANGO_DEBUG=False` with a placeholder key) deliberately refuses to start.
- Leave `AI_SERVICE_MODE=mock`. The remote values in the example are placeholders and are not used in mock mode.

**3. MySQL database and account.** In a MySQL client, as an administrator. Replace the user name and password with your own:

```sql
CREATE DATABASE ai_video_generator CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'geetha_app'@'localhost' IDENTIFIED BY 'choose-a-password';
GRANT ALL PRIVILEGES ON ai_video_generator.* TO 'geetha_app'@'localhost';
-- Only for running the tests: the test runner creates and drops this database.
GRANT ALL PRIVILEGES ON test_ai_video_generator.* TO 'geetha_app'@'localhost';
-- Only for the two row-lock tests, which read INFORMATION_SCHEMA.INNODB_TRX.
GRANT PROCESS ON *.* TO 'geetha_app'@'localhost';
```

If you change `MYSQL_DATABASE`, the test database is `test_` + that name.

**4. Tables and checks.**

```
cd backend
python manage.py migrate
python manage.py check
```

- `migrate` creates Django's tables and `generation_jobs` (migration `generator.0001_initial`), in the database named in `.env`.
- `python manage.py createsuperuser` is optional. It is needed only for the read-only job admin.

## Running locally

```
cd backend
python manage.py runserver
```

This starts:

- **The frontend** at <http://127.0.0.1:8000/>, served from `../frontend/` by Django. This is for development only.
- **The API** at <http://127.0.0.1:8000/api/v1/>.
- **The Django admin** at <http://127.0.0.1:8000/admin/>.
- **One generation worker thread**, because `RUN_WORKER_IN_PROCESS` is on by default. One command runs the whole application.

For local development, set `DJANGO_DEBUG=True` or provide a real `DJANGO_SECRET_KEY`; otherwise startup fails as described above.

## Generation worker

Jobs are processed by `apps.generator.worker`. There are two ways to run it:

- **Inside the development server.** `python manage.py runserver` starts one worker thread when `RUN_WORKER_IN_PROCESS` is true.
  - The thread starts only in the auto-reloading child process, so it starts once, not twice. With `runserver --noreload` the worker is **not** started; run it separately.
  - It is a daemon thread, so it stops when the server stops or reloads.
- **As a separate process:**

  ```
  cd backend
  python manage.py run_generation_worker [--poll-seconds N]
  ```

  - `--poll-seconds` sets how long the worker waits between checks when the queue is empty. The default is `2.0`; decimals are allowed. It must be greater than 0 (0, negative, `nan` and `inf` are refused).
  - Stop it with Ctrl+C. It prints `Worker stopped.`, and a job it was working on is marked `failed` (`worker_interrupted`).
  - To use only separate workers, set `RUN_WORKER_IN_PROCESS=False` for the web server.

Several workers, in-process and separate, may run at the same time against the same database. Each job is claimed by exactly one worker. See [Worker reliability](#worker-reliability).

Without a running worker, run the recovery cycle with:

```
cd backend
python manage.py expire_stale_jobs [--dry-run]
```

It fails queued jobs past the queue timeout (`queue_timeout`) and abandoned processing jobs (`worker_interrupted`), and prints both counts. It is safe to run repeatedly and while workers run; schedule it (for example every few minutes) on a deployment that may have no worker running.

## Running the tests safely

```
cd backend
python manage.py test tests
```

Last full run (21 October 2026, Section 3.8):
- **587 tests**, all passed, in about a minute.
- **2 skipped:** symbolic-link tests that need Windows Developer Mode or administrator rights. Directory-junction tests cover the same behaviour.
- **Coverage:** no coverage tool is installed, so no coverage percentage has been measured.

The suite is built so it never touches real data:

- **Temporary MySQL database.**
  - Django's test runner creates a separate database named `test_` + `MYSQL_DATABASE` (by default `test_ai_video_generator`), runs every test there, and drops it at the end.
  - The real database is never used, so the MySQL account needs permission to create and drop that test database.
  - `tests/test_safety.py` fails if the active database name does not start with `test_`.
- **Temporary media folder.**
  - Every test that can write files (`tests/base.py`, `TempMediaMixin`) points `MEDIA_ROOT` at a new temporary folder, which is deleted afterwards.
  - The tests refuse to run if file storage still points at the real `media/` folder. This check is an explicit `raise`, not an `assert`, so it also holds under `python -O`.
- **No real AI service.**
  - `tests/__init__.py` forces `AI_SERVICE_MODE=mock`, an unroutable `AI_SERVICE_BASE_URL` and an empty `AI_SERVICE_API_KEY` for the whole run, whatever `.env` contains.
  - `tests/test_safety.py` checks this.
  - The remote client is tested only against a fake service on `127.0.0.1`.
- **Leftover test database.** If a previous run was interrupted and the test database still exists, Django asks before deleting it. `python manage.py test --noinput` deletes it without asking. Only the `test_` database is affected.
- **Other test behaviour:**
  - The settings tests start Django in subprocesses with controlled environment variables. They print only pass/fail results, never a key.
  - Some worker tests open several real database connections, and close (`KILL`) only sessions they opened themselves on the test database. The two row-lock tests read MySQL's `INFORMATION_SCHEMA.INNODB_TRX`, which needs the `PROCESS` privilege.
  - Tests that need FFmpeg are skipped if it is not available.

Run one module with, for example, `python manage.py test tests.test_worker_recovery`. The suite also passes in reverse and shuffled order (`--reverse`, `--shuffle`).

## API endpoints

All endpoints are under `/api/v1/`. Request and response details are in [docs/api-documentation.md](../docs/api-documentation.md).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `health/` | Returns `{"status": "ok"}` if Django is responding. It does not check the database or the AI service. |
| `GET` | `csrf/` | Returns `{"csrf_token": "..."}` and sets the `csrftoken` cookie. |
| `GET` | `profiles/` | Lists the model profiles with their allowed sizes, frame counts, fps and options. |
| `POST` | `generations/text-to-video/` | Creates a text-to-video job. Returns `202` with `job_id`, `mode`, `status` (`queued`), `created_at` and `status_url`. |
| `POST` | `generations/image-to-video/` | Creates an image-to-video job (multipart). Same response as above. |
| `GET` | `generations/` | Lists the caller's jobs, newest first. Paginated: `page`, `page_size` (default 20, maximum 50). Optional `status` and `mode` filters; unknown values are ignored. |
| `GET` | `generations/<job_id>/` | Job status, settings, timestamps, `result` (when completed) or `error` (when failed). |
| `POST` | `generations/<job_id>/cancel/` | Cancels the caller's own `queued` job (it becomes `failed`, code `cancelled`). `409` `not_cancellable` once it is processing or finished. |
| `GET` | `generations/<job_id>/video/` | Streams the MP4. Add `?download=1` to download it as an attachment. Named `geetha-<job_id>.mp4` (`geetha-mock-<job_id>.mp4` for mock output). |

- **Caching.** Job creation, details, history, videos and `csrf/` responses, including their errors, carry `Cache-Control: private, no-store` and `Vary: Cookie`, so shared caches never store one user's data.

- **Unknown paths.** Unknown `/api/` paths return a JSON `404` in the standard error format.
- **Request IDs.** Every response carries an `X-Request-ID` header. A safe incoming `X-Request-ID` is reused; otherwise a new one is generated.

## CSRF, ownership, rate limits and errors

- **CSRF.**
  - Every `POST` needs a CSRF token, from guests as well as signed-in users.
  - Get the token from `GET /api/v1/csrf/` and send it in the `X-CSRFToken` header, or as the `csrfmiddlewaretoken` form field.
  - A missing or invalid token returns `403` (`forbidden`, message starting with `CSRF Failed`) before any validation.
  - `GET` requests need no token.
- **Ownership.**
  - **Guests:** most users are guests, and their jobs belong to their Django session cookie. The session is created when the first job is created; read requests never create one.
  - **Signed-in users:** jobs belong to the user account. At present only staff accounts can sign in, through the Django admin login; there is no sign-up or login API.
  - **Visibility:** a job that belongs to someone else returns `404`, exactly like a missing job.
  - **Lost cookie:** a guest who clears their cookies loses access to their earlier jobs.
- **One active job per owner.**
  - An owner (signed-in user, or guest session) may have one `queued` or `processing` job at a time, across both generation endpoints.
  - Another creation request meanwhile returns `409` (`generation_in_progress`) and creates nothing: no job, no stored image.
  - Simultaneous requests from one owner create exactly one job. The check and the insert run under a per-owner MySQL named lock (`GET_LOCK`, see `apps/generator/active_jobs.py`), so this holds across processes, without row locks that could interfere with the worker.
  - The limit is `GENERATION_MAX_ACTIVE_JOBS_PER_OWNER = 1` in `config/settings.py` (not an environment variable). It relies on job creation committing at once, so `ATOMIC_REQUESTS` must stay off.
  - The test suite raises the limit only for tests about other features, which post several times without a worker; `tests/test_active_jobs.py` tests the real limit.
- **Rate limits.**
  - **Scope:** only the two generation endpoints are limited, and they share one allowance: **5 requests per minute** and **30 per hour**, both applied at once.
  - **Identity:** signed-in users are counted by user ID. Guests are counted by the direct client address (`REMOTE_ADDR`); `X-Forwarded-For` is ignored.
  - **What counts:** the limit is checked after CSRF and before validation. CSRF-rejected requests do not count; requests that fail validation, or get `409` because a job is active, do. `GET` endpoints are never limited.
  - **Response:** a limited request creates nothing and returns `429` (`rate_limited`) with a `Retry-After` header in seconds.
  - **Counters:** stored in Django's default in-memory cache. They reset when the server restarts and are not shared between processes.
- **Errors.**
  - Every API error uses one shape. `fields` is present only on validation errors:

    ```json
    {"error": {"code": "unsupported_setting", "message": "...", "request_id": "...", "fields": {"settings": {"width": ["..."]}}}}
    ```

  - Status codes:

    | Status | Meaning |
    |---|---|
    | `400` | Validation |
    | `403` | CSRF |
    | `404` | Not found / not yours / video not ready |
    | `405` | Method not allowed |
    | `409` | `generation_in_progress`: the owner already has an active job. `not_cancellable`: the job is no longer queued. |
    | `413` | Too large |
    | `415` | `unsupported_media_type`: an image-to-video request that is not multipart or form data |
    | `416` | `range_not_satisfiable`: a video byte range outside the file |
    | `429` | Rate limited |
    | `500` | `internal_error` |

  - Tracebacks and file paths are never returned.
  - Failed jobs report `error.code` with a message that is safe to show to users. The full list with exact messages is in [Job error codes](../docs/api-documentation.md#job-error-codes):

    | Set by | Codes |
    |---|---|
    | Backend, any mode | `cancelled`, `queue_timeout`, `worker_interrupted`, `unsupported_setting`, `invalid_job_settings`, `input_missing`, `internal_error` |
    | AI client, either mode | `invalid_output` |
    | Mock client | `mock_unavailable`, `mock_failed` |
    | Remote client | `service_not_configured`, `service_unavailable`, `service_auth_failed`, `service_rejected`, `service_error`, `invalid_response`, `service_timeout`, `download_failed`, `output_too_large`, `generation_failed` |

  - When the remote service reports a job `failed` with its own safe code (1–50 characters from `a-z`, `0-9`, `_`), that code is passed through, so clients must handle unknown codes. Its message is never passed through.

## Generation requests

Both endpoints accept an optional `settings` object, validated against a **model profile** (`apps/generator/profiles.py`). Omitted values use the profile's defaults, and any value the profile does not allow returns `400` `unsupported_setting`. A `seed` outside 0–2147483647 or an unknown `style` is a plain `400` `validation_error`.

| Setting | Allowed values |
|---|---|
| `model_profile` | `wan2.1-t2v-1.3b-480p` (text to video, default) or `wan2.1-i2v-14b-480p` (image to video, default); the profile must match the endpoint's mode |
| `width` × `height` | `832 × 480` (default) or `480 × 832` |
| `frame_count` | 17–81 and of the form 4n + 1 (default 81) |
| `fps` | `16` only |
| `seed` | integer 0–2147483647, or `null` |
| `style` | `cinematic`, `realistic`, `anime`, `3d_animation`, or `null` |
| `enhance_prompt` | `true`/`false` (default `false`); not available for the image-to-video profile |

**Text to video** (`POST generations/text-to-video/`, as JSON, form or multipart):

- `prompt` is required.
  - It must be a string; numbers and booleans are rejected, not converted.
  - Surrounding whitespace is trimmed, and the result may be at most 1000 characters.
  - It must contain visible text: a prompt made only of spaces or invisible characters is rejected (`validation_error`).
  - Any language and emoji are accepted unchanged.
- `settings` is optional: a JSON object in a JSON body, or the JSON text of an object in a form or multipart body.

**Image to video** (`POST generations/image-to-video/`, as `multipart/form-data`):

- `image` is required. See [Upload and size limits](#upload-and-size-limits).
- `prompt` is optional.
  - It follows the same rules as above, except that missing, empty or whitespace-only values are accepted and stored as empty.
- `settings` is an optional **JSON-encoded string** containing the settings object.
- **Stored filename.** The image is stored under a server-generated name, `<keyword>-<uuid>.<ext>`. The keyword is up to two ASCII words taken from the prompt, or `image` if there are none, and the extension comes from the detected format. The client's filename is never used.

**Field rules (both endpoints).** Full details are in the [API documentation](../docs/api-documentation.md#request-field-rules).
- **Unknown fields.** Only the fields above are accepted, plus Django's `csrfmiddlewaretoken` form field. Any other field, at the top level or inside `settings` (for example a misspelt `widht`, or an `image` sent to text to video), returns `400` `unknown_field` with every unknown field at that level listed (top-level fields are checked before `settings`).
- **Setting types.** Values must already have the right JSON type and are never converted:
  - `width`, `height`, `frame_count`, `fps` and `seed` are integers (so `"832"` and `832.0` are rejected);
  - `enhance_prompt` is `true` or `false`;
  - `model_profile` and `style` are strings (`seed` and `style` may also be `null`).

  A wrong type, or `settings` in the wrong format, returns `400` `validation_error`.
- **Nothing is created.** Rejected requests create no job and store no upload.

## Upload and size limits

Units: 1 MiB = 1,048,576 bytes; 1 megapixel = 1,000,000 pixels. API messages write MiB as "MB".

| Limit | Value | Enforced by | Response |
|---|---|---|---|
| Whole generation request | 26 MiB declared `Content-Length` (`GENERATION_MAX_REQUEST_BYTES`) | `GenerationRequestSizeMiddleware`, before the body is read and before CSRF and rate limiting | `413` `request_too_large` |
| Uploaded image | 25 MiB (`MAX_IMAGE_BYTES`) | Image validation | `400` `image_too_large` |
| Image dimensions | at most 4096 px wide and 4096 px high, and at most 16 megapixels | Image validation, from the file headers | `400` `image_dimensions_too_large` |
| Non-file body (JSON/form bodies and multipart text fields) | 2.5 MiB, Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` default; it does not limit uploaded files | Django request parsing | `413` `request_too_large` |
| Form fields / files per request | 1000 / 100 (Django defaults) | Django request parsing | `400` `invalid_request` |

- **Image formats.** PNG, JPEG and WebP (lossy, lossless and extended) are accepted.
  - The format and dimensions are read from the file contents, never from the file name or declared content type.
  - Other formats return `unsupported_image_type`. Empty, damaged or truncated headers return `invalid_image`.
  - Pixel data is not decoded, so an image with valid headers but corrupt data can still be accepted.
- **Request-size check.** Only the declared `Content-Length` is checked. A malformed value returns `400` `invalid_request`.
  - The check bounds what Django reads, but it does not stop a client from sending more data to the web server. A deployment needs the same limit at the proxy (see below).
- **Rejected uploads.** An oversized request creates no job, stores no file, and uses no rate-limit allowance.

## Mock and real AI generation

The worker gets its AI client from `ai_service/client/factory.py`, based on `AI_SERVICE_MODE`.

- **`mock` (default, the only mode that has been run end to end).** `MockClient` does **not** run an AI model.
  - After a 3-second delay, it uses FFmpeg to render a synthetic `testsrc2` test pattern with the requested width, height, frame count and fps, and saves it as an MP4.
  - The prompt and image are not used.
  - Jobs are tagged `mock-<id>`, and the API marks their result with `"is_mock": true` so the frontend can label the output as a test video.
  - Without FFmpeg, jobs fail with `mock_unavailable`.
- **`remote` (not integrated).** `RemoteClient` is an HTTP client for a protected generation service intended to run Wan 2.1 in Google Colab. It submits a job, polls its status and downloads the MP4, authenticated with `AI_SERVICE_API_KEY`.
  - **No such service is in this repository.** `ai_service/colab/` exists but is empty (so Git does not track it); the contract the service must implement is the docstring of `ai_service/client/remote.py`.
  - The client has been tested only against simulated responses.
  - No model profile has been run on the Colab T4 (`t4_verified` is `false` for all profiles), and Qwen3 prompt enhancement is not implemented; `enhance_prompt` is only validated and passed on.
  - With `remote` mode set but no URL or key, jobs fail with `service_not_configured`.
  - **Limits** (see the module docstring of `ai_service/client/remote.py`):
    - **Overall deadline.** Submission, polling and the download together must finish within `AI_SERVICE_MAX_WAIT`. Each request's timeout is capped by the time left, polling sleeps stop at the deadline, and the download checks it after every chunk, so it can be overrun by at most one network read (`AI_SERVICE_TIMEOUT`). Expiry fails the job with `service_timeout` ("The video took too long to generate.").
    - **Video size.** At most `AI_SERVICE_MAX_VIDEO_BYTES`. A larger `Content-Length` is refused before reading, and received bytes are counted, so a missing or wrong `Content-Length` cannot bypass it (`output_too_large`). A body shorter than its `Content-Length`, or a stalled or dropped download, fails with `download_failed`. Any partly written file is deleted; nothing else is.
    - **Job IDs.** The service's job ID must be 1 to 64 ASCII letters, digits, `_` or `-`, starting with a letter or digit, and must not start with `mock-` (reserved for the mock client). Anything else fails with `invalid_response` before it is stored or used in a URL, and no further request is made.
    - **Replies.** JSON replies must be objects of at most 64 KiB (`invalid_response` otherwise). The service's own error messages and HTTP error bodies never reach users; logs keep a short, truncated summary, and never the API key or a rejected job ID.
  - The client is tested against a local fake HTTP service (`tests/test_remote_client.py`); no real service or credentials are needed.

Every output, mock or remote, is checked to be an MP4 (`ftyp` header, at least 1 KB) before it is stored.

## Worker reliability

Jobs are leased through their existing `status` and `updated_at` columns; there are no extra tables. Full details are in [docs/architecture.md](../docs/architecture.md#generation-worker-implemented).

| Setting (in `config/settings.py`) | Value | Meaning |
|---|---|---|
| `WORKER_HEARTBEAT_SECONDS` | 30 | A heartbeat thread refreshes a `processing` job's `updated_at` this often while its worker works on it. |
| `WORKER_STALE_AFTER_SECONDS` | 300 | A `processing` job with no heartbeat for this long counts as abandoned. |
| `WORKER_RECOVERY_INTERVAL_SECONDS` | 60 | How often each running worker looks for abandoned jobs and expired queued jobs (and once at start-up). |
| `GENERATION_QUEUE_TIMEOUT_SECONDS` | 1800 (environment) | A `queued` job older than this fails with `queue_timeout` and is never started. |

- **Claiming.** `SELECT ... FOR UPDATE SKIP LOCKED` ensures two workers never claim the same job.
- **State changes.** Status changes always start from the stored status: every writer uses a conditional `UPDATE`, `transition_to()` raises `StaleJobState` if the job changed after it was loaded, and `save()` locks the row and refuses to overwrite a newer status or re-create a deleted job. Details in [docs/architecture.md](../docs/architecture.md#generation-worker-implemented).
- **Recovery.**
  - Abandoned jobs, whose worker crashed, was killed or lost power, are marked `failed` with `worker_interrupted`.
  - This is done by one conditional `UPDATE`, so another worker's active job is never touched and each abandoned job is failed only once.
  - After a hard crash, a job therefore stays `processing` for about 5 minutes before it is failed.
- **Queue timeout.**
  - In the same cycle, `queued` jobs older than `GENERATION_QUEUE_TIMEOUT_SECONDS` are marked `failed` with `queue_timeout`; a worker never claims such a job, even before it is marked.
  - One conditional `UPDATE` (`WHERE status = 'queued'`): a job being claimed is row-locked, so expiry waits and then skips it. Repeated or concurrent runs expire each job only once.
  - **Limitation:** expiry needs a running worker or a scheduled `manage.py expire_stale_jobs`. Otherwise a queued job stays queued, and blocks its owner, until the owner cancels it.
  - The timeout counts from creation, including time waiting behind other jobs, so keep it above the longest expected wait for a busy worker.
  - **Known limitation: remote deadline.** A worker runs one job at a time, and one remote generation can take up to `AI_SERVICE_MAX_WAIT + AI_SERVICE_TIMEOUT` (1830 s by default), longer than the default queue timeout of 1800 s. With one worker, a job queued behind a long remote generation can therefore fail with `queue_timeout` before it starts. The two settings are not checked against each other. Tune them once real generation times are known, or run more workers. Mock mode is unaffected (120 s per render).
- **Cancellation.** Owners can cancel a `queued` job (`POST generations/<job_id>/cancel/`); it becomes `failed` with `cancelled`. It is also one conditional `UPDATE`, so a worker's claim and a cancellation never both succeed. Processing jobs cannot be cancelled.
- **Results.**
  - A worker records `completed` or `failed` only while the job is still `processing`. A late result for a job that was already recovered is discarded, and the file it just wrote is deleted.
  - Only that file is ever deleted: never another job's video, and never an input image.
- **Database reconnection.**
  - The worker checks its own database connections. Django normally does this only around web requests.
  - The heartbeat thread pings its connection before each heartbeat and replaces it after any failure. A MySQL restart, a network drop or a killed session therefore only costs heartbeats until the database answers again.
  - Retries happen once per heartbeat interval, never faster. An error is logged if heartbeats keep failing for longer than the stale limit.
  - Before recording a result, the worker also checks its connection. If the write fails because of a connection error, the job is re-read on a new connection, and the write is retried once only if the job is still `processing`.
  - If the job cannot be re-read, the output file is kept and the job is left for recovery. The job ID and file name are logged.
- **Errors.** An unexpected error while processing a job is logged and the worker keeps running; the job is recovered once its lease goes stale.
- **Remaining risks:**
  - **Clock synchronisation across machines.** Lease timestamps come from each worker host's clock.
    - On one machine, the current setup, this is exact.
    - With workers on several machines, a host whose clock runs more than about 4.5 minutes ahead would fail other workers' active jobs.
    - Keep hosts synchronised (NTP) until leases use the database server's clock; that is planned as a separate change.
  - **No cancellation of running work.** Only queued jobs can be cancelled. A worker whose lease was lost keeps generating until the AI client finishes or times out, and only then discards the result.
  - **Orphaned files are possible.** If the worker process is killed between saving a video and recording it, or the outcome of recording is unknown, the file stays in `media/outputs/`. Nothing cleans these up automatically; `manage.py find_orphaned_media` reports them (see [Media audit](#media-audit-orphaned-files)).

## Media audit (orphaned files)

Stored files can outlive their database records. Django never deletes a file when its row goes, so deleting a job in the admin, or deleting a user (whose jobs are removed with them), leaves the job's input image and video behind. A worker that is killed between saving a video and recording it, or whose recording outcome was unknown, also leaves its file. To find such files:

```
cd backend
python manage.py find_orphaned_media [--grace-minutes N] [--json] [--show-file-names]
```

**It only reports. It never modifies, moves or deletes a file or a database record, and has no option to.** Files are listed and `stat()`-ed, never opened; the database is only read. Removing anything is a manual decision.

Every file under `MEDIA_ROOT` is put in one category:

| Category | Meaning |
|---|---|
| Referenced | A database file field names it. |
| Orphan candidate | Nothing refers to it, it is older than the grace period, and it is not the output of a `queued` or `processing` job. The reason says which: an output of a deleted job, an output its (finished) job does not refer to, an unreferenced input image, or a file outside `inputs/` and `outputs/`. |
| Possibly in use | Nothing refers to it **yet**: it was modified within the grace period (default 60 minutes), or it is named after the output of a job that is still active. The worker saves a finished video just before recording it, and an upload is written just before its job row, so such files are normal. They are never counted as orphans; re-run the audit later. |
| Ignored | Repository placeholders such as `.gitkeep`. |

It also lists database references whose file is missing (field, job ID and status), references that would point outside `MEDIA_ROOT` (reported, never resolved on disk), and symbolic links or Windows junctions, which are never followed, so the scan cannot leave `MEDIA_ROOT`. The worker's own scratch files are in the system temporary folder, never in `MEDIA_ROOT`.

- **Output.** Paths relative to `MEDIA_ROOT`, sizes, modification times (UTC), counts and total sizes per category; `--json` for a machine-readable report. Prompts, owners, session keys and request IDs are never shown. Stored input names begin with a keyword from the prompt, so that part is masked (`inputs/2026/10/*-<hex>.png`; the hex part is unique, so the pattern still finds the file) unless `--show-file-names` is given.
- **Unsafe `MEDIA_ROOT` is refused.** If `MEDIA_ROOT` is a drive or file-system root, or contains the application (`BASE_DIR` or `FRONTEND_DIR`), the command stops with an error before scanning anything. Every source file there would otherwise be listed as an orphan candidate. One way this happens is `MEDIA_ROOT=` left empty in `.env`, which makes it the project folder.
- **Safe use.**
  1. Run the report.
  2. Treat "orphan candidates" as items to review, not a delete list. "Possibly in use" files must be left alone; re-run the audit later.
  3. Before removing anything by hand, check the job in the admin and keep a backup.
  4. Re-run the report afterwards. It should show no new "missing file" references.
- **Limits.** Only file fields on Django's default storage under `MEDIA_ROOT` are checked (today: `GenerationJob.input_file` and `output_file`). A file can only be "possibly in use" from its age and name, so choose a grace period longer than the longest upload or save.
- **Retention policy (development and final-year demo).** Uploaded images and generated videos are kept indefinitely, and nothing deletes media automatically: failed and cancelled jobs keep their input images, and deleting a job or user leaves its files. This command is the read-only report, and it has no deletion option. Any deletion is a manual step, made only after reviewing its report. The policy must be revisited before public deployment. See [docs/database-design.md](../docs/database-design.md#retention).

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `ImproperlyConfigured: DJANGO_SECRET_KEY is missing or blank`, or `... published placeholder value` | `DJANGO_DEBUG` is off. For local work set `DJANGO_DEBUG=True` in `.env`, or set a real key (see [Configuration](#configuration)). |
| `ImproperlyConfigured: AI_SERVICE_MODE must be 'mock' or 'remote'.` | A misspelt mode in `.env` or the environment. |
| `ImproperlyConfigured: ... must be a whole number of at least 1.` | A numeric setting (`AI_SERVICE_TIMEOUT`, `AI_SERVICE_POLL_INTERVAL`, `AI_SERVICE_MAX_WAIT`, `AI_SERVICE_MAX_VIDEO_BYTES`, `GENERATION_QUEUE_TIMEOUT_SECONDS`) is not a positive whole number. |
| MySQL `OperationalError` 1045 ("Access denied") or 2003 ("Can't connect") | Wrong `MYSQL_*` values, or MySQL is not running. |
| `Got an error creating the test database` | The MySQL account may not create `test_ai_video_generator`. Add the grant from [Installation](#installation). |
| `Bad Request (400)` for every page (with `DJANGO_DEBUG=True`, a `DisallowedHost` error page) | The host name in the address bar is not in `DJANGO_ALLOWED_HOSTS` (default `127.0.0.1,localhost`). |
| `403`, message starting with `CSRF Failed` | Open the app at <http://127.0.0.1:8000/>, served by Django. Don't open it from `file://` or another server or port: the page and the API must share one origin. |
| Jobs stay `queued` | No worker is running. Either `RUN_WORKER_IN_PROCESS=False`, or the server was started with `runserver --noreload`, which never starts the in-process worker. Start `python manage.py run_generation_worker`. |
| A job fails with `mock_unavailable` | FFmpeg was not found. Install `imageio-ffmpeg` from `requirements.txt`, or put `ffmpeg` on `PATH`. |
| A job stays `processing`, then fails with `worker_interrupted` after about 5 minutes | Its worker stopped: Ctrl+C, a crash, or `runserver` reloading after a Python file was edited (the in-process worker restarts with it). Lease recovery is working as designed. |
| `409` `generation_in_progress` | The owner already has a queued or processing job. Wait for it, or cancel it while it is still queued. |
| An upload is refused with `400` or `413` | Images must be PNG, JPEG or WebP, at most 25 MiB, 4096 px per side and 16 megapixels. See [Upload and size limits](#upload-and-size-limits). |

## Production deployment

The settings are development-oriented. Before deploying:

- **Debug and secret key.** Set `DJANGO_DEBUG=False` and a unique, random `DJANGO_SECRET_KEY`; startup fails otherwise. Set `DJANGO_ALLOWED_HOSTS` to the real host names.
- **HTTPS.** Serve the site over HTTPS only. The settings do **not** yet enable HTTPS-related options, and none of them can be set through environment variables, so adding them is a settings change. `python manage.py check --deploy` currently reports these four as not set:

  | Django setting | `check --deploy` warning |
  |---|---|
  | `SECURE_SSL_REDIRECT` | `security.W008` |
  | `SECURE_HSTS_SECONDS` | `security.W004` |
  | `SESSION_COOKIE_SECURE` | `security.W012` |
  | `CSRF_COOKIE_SECURE` | `security.W016` |

  Also set `CSRF_TRUSTED_ORIGINS` if the site is reached through another origin.
- **HTTPS reverse proxy.** If a proxy (nginx, a load balancer, a tunnel) terminates TLS and forwards plain HTTP to Django, set `DJANGO_BEHIND_HTTPS_PROXY=True`. Django then treats a request as HTTPS when it carries `X-Forwarded-Proto: https` (`SECURE_PROXY_SSL_HEADER`). Without it, the history API's absolute `next`/`previous` links use `http://`, and browser `POST`s fail the CSRF check because their `https://` `Origin` does not match. Enable it only when all of the following hold, because any client can send this header:
  - Django is reachable **only** through the proxy (bind it to `127.0.0.1` or a private network; no direct port is exposed).
  - The proxy **sets** `X-Forwarded-Proto` on every request, replacing anything the client sent. Do not append to it: Django reads only the first value of a comma-separated list.
  - The proxy passes the original `Host` header unchanged, and `DJANGO_ALLOWED_HOSTS` lists the public host name. `X-Forwarded-Host` is never trusted (`USE_X_FORWARDED_HOST` stays off).

  For nginx, for example:

  ```
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto $scheme;
  ```

  Leave the setting off when Django is served directly or over plain HTTP; `X-Forwarded-Proto` is then ignored.
- **Request body limit at the proxy.** Set the same 26 MiB limit at the reverse proxy or hosting layer, for example `client_max_body_size 26m;` in nginx. Django checks only the declared size, and an ASGI server buffers the whole body before Django runs.
- **Proxies and rate limits.** Behind a reverse proxy, every guest appears to come from the proxy's address and would share one rate-limit allowance. Revisit the guest identity rule in `apps/generator/throttles.py` before deploying behind a proxy.
- **Shared cache.** With more than one web process, configure a shared cache (`CACHES`, for example Redis or Memcached) so rate-limit counters are shared. The default in-memory cache is per process.
- **Web server and files.** Use a production WSGI/ASGI server, not `runserver`.
  - Serve `frontend/` and static files with a real web server. Django's built-in serving is for development.
  - Keep `media/` private; videos are delivered only through the API's ownership-checked endpoints.
- **Workers.** Run one or more `python manage.py run_generation_worker` processes under a process supervisor, and set `RUN_WORKER_IN_PROCESS=False` for the web servers. All workers must share the database and keep synchronised clocks.
- **Database account.** Give the production MySQL account only what the application needs. Creating and dropping databases is needed only for running the tests.
