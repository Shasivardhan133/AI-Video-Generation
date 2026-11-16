# API contracts

**Status: implemented in `backend/apps/generator/` (development).** The examples below match the current Django REST Framework implementation; they were last checked against the source and the running mock-mode server on 21 October 2026. There is no sign-up or login API, and accounts and production media storage are open decisions. Media retention for the demo is decided: keep everything, with manual review only (see [database-design.md](database-design.md#retention)).

Generation currently runs in **mock mode**: videos are FFmpeg test patterns, flagged `result.is_mock: true`. No real AI service exists yet, so no response in this document comes from Wan 2.1.

## Endpoint summary

| Method | Path | Purpose | CSRF token | Private caching | Success |
|---|---|---|---|---|---|
| `GET` | `/api/v1/health/` | [Liveness check](#health) | no | no | `200` |
| `GET` | `/api/v1/csrf/` | [CSRF token](#csrf-token) and `csrftoken` cookie | no | yes | `200` |
| `GET` | `/api/v1/profiles/` | [Model profiles](#model-profiles): the settings the API accepts | no | no | `200` |
| `POST` | `/api/v1/generations/text-to-video/` | [Create a text-to-video job](#create-a-text-to-video-job) | **yes** | yes | `202` |
| `POST` | `/api/v1/generations/image-to-video/` | [Create an image-to-video job](#create-an-image-to-video-job) (multipart) | **yes** | yes | `202` |
| `GET` | `/api/v1/generations/` | [History](#list-generation-history), paginated | no | yes | `200` |
| `GET` | `/api/v1/generations/{job_id}/` | [Status and details](#check-generation-status) | no | yes | `200` |
| `POST` | `/api/v1/generations/{job_id}/cancel/` | [Cancel a queued job](#cancel-a-queued-job) | **yes** | yes | `200` |
| `GET` | `/api/v1/generations/{job_id}/video/` | [Play or download the video](#retrievedownload-a-generated-video) | no | yes | `200`, or `206` for a range |

Notes:

- Every `GET` endpoint also answers `HEAD`.
- `{job_id}` must be a UUID. Any other value matches no route and gets the standard `404` `not_found`.
- Only the two creation endpoints are [rate limited](#rate-limits).
- Outside `/api/v1/`, Django also serves the frontend at `/` (see [frontend/README.md](../frontend/README.md)) and the read-only job admin at `/admin/`.

## Common conventions

- Base path: `/api/v1/`.
- JSON responses use `Content-Type: application/json`.
- Image submission uses `multipart/form-data`.
- Job identifiers are opaque UUIDs.
- Timestamps are ISO 8601 in UTC with a `Z` suffix, with microseconds when they are not zero (for example `2026-10-21T02:18:36.188947Z`). The examples below round them to whole seconds.
- Every API URL ends with a slash (`/`). A `GET` or `HEAD` request without the trailing slash is redirected (`301`) to the URL with the slash. Any other method (`POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`) sent without the trailing slash returns `404` with code `not_found` and the message `Not found. API URLs must end with a slash.`, in the standard [error shape](#error-shape). This happens with `DEBUG` on or off, before the CSRF check and the rate limit, so nothing is created and no rate-limit allowance is used.
- A successful creation returns `202 Accepted` because generation is asynchronous.
- Every response carries an `X-Request-ID` header. A client may send its own `X-Request-ID` (1–64 characters from `A-Z a-z 0-9 . _ -`); otherwise the server generates one.
- Caching: responses that belong to one session or user carry `Cache-Control: private, no-store` and `Vary: Cookie`, errors included, so neither a shared cache (proxy, CDN) nor the browser cache stores them. This covers job creation, job details, history pages, videos and `csrf/`. `health/` and `profiles/` are the same for everyone and carry no `Cache-Control` header. Errors that are answered before any view runs carry the same private headers: an oversized request or malformed `Content-Length`, a missing trailing slash, an unknown `/api/` path, or a job ID that is not a UUID.
- Ownership: signed-in users see only their own jobs. Anonymous users own jobs through their browser session cookie. A job owned by someone else returns `404`, exactly like a missing job, so job IDs cannot be probed.
- CSRF: every `POST` (from guests and signed-in users alike) needs a CSRF token. `GET /api/v1/csrf/` returns `{"csrf_token": "..."}` and sets the `csrftoken` cookie; send the token back in the `X-CSRFToken` header (or as the `csrfmiddlewaretoken` form field). A missing, invalid, or mismatched token, or a cross-origin `Origin`, returns `403` with code `forbidden` and a message starting with `CSRF Failed`, before any request validation runs. `GET` requests need no token.
- Status codes: validation errors use `400`; CSRF failures use `403`; missing jobs, other users' jobs, videos that are not ready, and unknown `/api/` paths use `404`; unsupported methods use `405`; an image-to-video request that is not `multipart/form-data` (or form-encoded) uses `415` with code `unsupported_media_type`; requests that are too large use `413` (see [Request and upload limits](#request-and-upload-limits)); unsatisfiable video byte ranges use `416` with code `range_not_satisfiable` (see [Retrieve/download a generated video](#retrievedownload-a-generated-video)); a generation request while the owner already has an active job uses `409` with code `generation_in_progress` (see [One active job per owner](#one-active-job-per-owner)), and cancelling a job that is no longer queued uses `409` with code `not_cancellable` (see [Cancel a queued job](#cancel-a-queued-job)); rate-limited generation requests use `429` (see [Rate limits](#rate-limits)); unexpected server errors use `500` with code `internal_error`. Every API error, including `404`, `413` and `500`, uses the [error shape](#error-shape) and never includes tracebacks or file paths.

## Job status values

| Status | Meaning |
|---|---|
| `queued` | Accepted and stored; waiting for the worker. This is the status returned on creation. |
| `processing` | The worker has claimed the job and sent it to the AI service. `started_at` is set. |
| `completed` | A verified MP4 output is stored. `completed_at` is set and `result` is present. |
| `failed` | Generation stopped. `completed_at` is set and `error` explains why. |

Allowed transitions: `queued → processing → completed`, and `queued → failed` or `processing → failed`. `completed` and `failed` are final. There is no `accepted` or `cancelled` status: a [cancelled](#cancel-a-queued-job) or [timed-out](#queue-timeout) queued job is `failed`, with error code `cancelled` or `queue_timeout`.

Interrupted jobs are recovered through a lease. While a worker processes a job, it refreshes the job's heartbeat every 30 seconds, and the job stays `processing`.

- **Abandoned jobs.** If a worker crashes, is killed, or loses power, its job's heartbeat stops. Once the heartbeat is more than 5 minutes old, the next running worker marks the job `failed` with code `worker_interrupted`. Workers check for such jobs when they start and every minute after that, so a client may see an interrupted job stay `processing` for about 5 minutes before it fails.
- **Active jobs.** A job whose heartbeat is current is never failed this way, including when another worker starts or restarts.
- **Database outages.** A brief database outage does not fail an active job, because the worker reconnects and resumes the heartbeat. An outage longer than 5 minutes can.
- **Stopping a worker.** A separate worker (`manage.py run_generation_worker`) stopped with Ctrl+C marks its current job `worker_interrupted` immediately. A job interrupted by stopping `runserver`'s built-in worker is recovered after the 5-minute timeout instead.
- **Late results.** If a job has already been marked `worker_interrupted`, its original worker cannot overwrite that outcome later: the late result is discarded, and any video file it produced is deleted.

### Queue timeout

A job that is still `queued` `GENERATION_QUEUE_TIMEOUT_SECONDS` after it was created (default 1800, 30 minutes; set it in the environment) is marked `failed` with code `queue_timeout` and the message "The video could not be started in time. Please try again." It never starts: a worker skips a job past the timeout even before it has been marked.

- **Who expires jobs.** Every running worker, as part of its recovery cycle: when it starts and every 60 seconds. The same cycle can be run by hand or from a scheduler with `python manage.py expire_stale_jobs` (it also recovers abandoned `processing` jobs and prints both counts; `--dry-run` only reports how many queued jobs are past the timeout).
- **Important limitation.** Expiry happens only while some process runs it. With no worker running and no scheduled `expire_stale_jobs`, a queued job stays `queued`, and keeps blocking its owner, indefinitely; the owner can [cancel it](#cancel-a-queued-job).
- **Safety.** One conditional `UPDATE` (`WHERE status = 'queued'`). A job a worker is claiming at that moment is row-locked; the expiry waits for the claim and then skips the job, which is `processing` by then. Repeated and concurrent runs expire each job only once.
- **Choosing the value.** It is measured from creation, so it includes time spent waiting behind other owners' jobs. Keep it above the longest expected wait for a busy worker, or queued jobs will expire while a worker is alive.
- **Interaction with the remote deadline (known limitation).** Each worker runs one job at a time, and in remote mode one job can take up to `AI_SERVICE_MAX_WAIT` plus one `AI_SERVICE_TIMEOUT` (by default 1800 + 30 seconds). The default queue timeout is also 1800 seconds. So with one worker, a job queued behind another owner's long remote generation can reach `queue_timeout` before it starts. Nothing checks or adjusts the two settings against each other. When real generation times are known, set `GENERATION_QUEUE_TIMEOUT_SECONDS` well above `AI_SERVICE_MAX_WAIT + AI_SERVICE_TIMEOUT`, or run more workers. Mock mode is not affected, because one mock render is limited to 120 seconds.

### Cancellation

Only a `queued` job can be cancelled, by its owner; see [Cancel a queued job](#cancel-a-queued-job). A cancelled job is `failed` with code `cancelled`.

## Create a text-to-video job

`POST /api/v1/generations/text-to-video/`

```json
{
  "prompt": "A slow cinematic shot of waves around a lighthouse at sunset",
  "settings": {
    "width": 832,
    "height": 480,
    "frame_count": 81,
    "style": "cinematic"
  }
}
```

`settings` and every field inside it are optional; omitted values use the model profile's defaults. In a JSON body, `settings` must be a JSON object, and every value must have the JSON type listed under [Request field rules](#request-field-rules). Unknown fields are rejected.

| Setting | Allowed values (profile `wan2.1-t2v-1.3b-480p`) |
|---|---|
| `model_profile` | `wan2.1-t2v-1.3b-480p` (default for text to video) |
| `width` × `height` | `832 × 480` (default) or `480 × 832` |
| `frame_count` | 17–81 and of the form 4n + 1 (for example 33, 49, 81); default 81 |
| `fps` | `16` only |
| `seed` | integer 0–2147483647, or `null` |
| `style` | `cinematic`, `realistic`, `anime`, `3d_animation`, or `null` |
| `enhance_prompt` | `true` or `false` (default `false`) |

`prompt` is required for text to video. It must be a JSON string (numbers, booleans, arrays and objects are rejected rather than converted), is trimmed of surrounding whitespace, is limited to 1000 characters after trimming, and must contain visible text: a prompt made only of spaces or invisible characters (such as zero-width spaces) is rejected with `validation_error`. Any language and emoji are accepted unchanged.

Response (`202 Accepted`):

```json
{
  "job_id": "6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006",
  "mode": "text_to_video",
  "status": "queued",
  "created_at": "2026-01-01T12:00:00Z",
  "status_url": "/api/v1/generations/6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006/"
}
```

## Create an image-to-video job

`POST /api/v1/generations/image-to-video/`

Multipart fields:

- `image`: required PNG, JPEG, or WebP file (WebP lossy, lossless, or extended), at most 25 MB, at most 4096 pixels wide and 4096 pixels high, and at most 16 megapixels in total. The type and dimensions are read from the file contents, never from the file name or declared content type. See [Request and upload limits](#request-and-upload-limits).
- `prompt`: optional supporting prompt, at most 1000 characters. Missing, empty, or whitespace-only prompts are accepted (stored as empty); a supplied prompt made only of invisible characters is rejected with `validation_error`.
- `settings`: optional text field containing the JSON text of a settings object (for example `{"frame_count": 49, "style": "anime"}`), validated against profile `wan2.1-i2v-14b-480p` (sizes `832 × 480` or `480 × 832`; prompt enhancement not available). An empty field is the same as leaving it out. See [Request field rules](#request-field-rules).

No other fields are accepted (see [Request field rules](#request-field-rules)).

The uploaded file is stored under a server-generated name, `<keyword>-<uuid>.<extension>`, where the keyword is up to two ASCII words taken from the prompt (for example `white-cat` for "A white cat walking in a garden", or `image` when no usable word exists) and the extension comes from the detected image format. The client's filename is never used.

The response (`202 Accepted`) has the same shape as above, with `"mode": "image_to_video"`.

Both creation endpoints return `409` (`generation_in_progress`) instead if the owner already has a queued or processing job; see [One active job per owner](#one-active-job-per-owner).

## One active job per owner

An owner may have only **one active job** (`queued` or `processing`) at a time. The owner is the signed-in user, or for guests the browser session, the same ownership used by the read endpoints. This applies to both creation endpoints together: an active image-to-video job also blocks a text-to-video request.

- **Response.** A creation request while the owner has an active job returns `409 Conflict`. Nothing is created: no job, no stored image, no worker work, and the session is unchanged.

```json
{
  "error": {
    "code": "generation_in_progress",
    "message": "Another video is still being generated. Wait for it to finish, then try again.",
    "request_id": "b1946ac92492d2347c6235b4d2611184"
  }
}
```

- **When it is checked.** After the request-size check, CSRF, the rate limit and request validation, just before the job is created. So a request with invalid fields still gets `400`, and a `409` request counts toward the [rate limit](#rate-limits), like a `400` does.
- **Finished jobs.** `completed` and `failed` jobs never block. As soon as the active job finishes, the next request is accepted.
- **Concurrent requests.** Several simultaneous requests from one owner (for example from two tabs) create exactly one job; the others get `409`. The check and the insert run under a per-owner database lock, so this holds across server processes. A request that cannot get the lock within 5 seconds, because another request from the same owner is still being created, also gets `409`.
- **Other owners.** Other guests and other signed-in users are never blocked, and a signed-in user's limit is shared across all of their browsers.
- **Known limits.** A job whose worker crashed stays `processing`, and keeps blocking its owner, until [stale-job recovery](architecture.md#generation-worker-implemented) fails it (about 5 minutes). A `queued` job blocks its owner until a worker processes it, it reaches the [queue timeout](#queue-timeout), or the owner [cancels it](#cancel-a-queued-job). A client that drops its session cookie gets a new guest session each time, so only the rate limit bounds it.
- **Lock errors.** If the per-owner lock cannot be taken because of a database error (not a timeout), the request fails with `500` `internal_error`, and nothing is created.
- **Frontend.** The page already sends one request at a time. If it still gets `409` (for example because another tab of the same session started a video), it shows the message, keeps the prompt and image, reloads history to find the active job, and enables Send again once that job finishes.

## Request field rules

These rules apply to both generation endpoints.

**Accepted fields.** No other field is accepted, at the top level or inside `settings`.

| Endpoint | Fields |
|---|---|
| Text to video | `prompt`, `settings` |
| Image to video | `image`, `prompt`, `settings` |

`csrfmiddlewaretoken` (Django's CSRF token sent as a form field) is also allowed.

**Unknown fields.** An unknown field returns `400` with code `unknown_field`. Every unknown field at one level is listed, at the same place in `fields` as in the request. Top-level fields are checked first: when the top level has an unknown field, unknown fields inside `settings` are reported only once it is fixed. Unknown fields are reported before any other validation error, so the code does not depend on what else is wrong:

```json
{
  "error": {
    "code": "unknown_field",
    "message": "Unknown field.",
    "request_id": "b1946ac92492d2347c6235b4d2611184",
    "fields": {"settings": {"duration": ["Unknown field."]}}
  }
}
```

For example, an `image` sent to text to video, or a misspelt setting such as `widht`, is rejected rather than silently ignored.

**`settings` format.**
- **JSON body:** a JSON object. A JSON string containing JSON is rejected with "Settings must be a JSON object."
- **Form or multipart body:** a text field containing the JSON text of an object. An empty field is the same as leaving `settings` out. Text that is not valid JSON returns "Settings must be valid JSON."; JSON that is not an object (an array, a number, a string, or `null`) returns "Settings must be a JSON object."
- `null` (in a JSON body) and a file uploaded as `settings` are rejected.
- All of these return `400` `validation_error`, with the message under `fields.settings`.

**Setting types.** Values must already have the right JSON type; they are never converted. A wrong type returns `400` `validation_error`, with the setting named under `fields.settings`.

| Setting | JSON type |
|---|---|
| `width`, `height`, `frame_count`, `fps` | Integers. `"832"`, `832.0` and `true` are rejected. |
| `seed` | An integer or `null`. |
| `enhance_prompt` | `true` or `false` only. `"true"`, `1`, `0` and `null` are rejected. |
| `model_profile` | A string. |
| `style` | One of the listed strings, or `null`. |

A value of the right type that the profile does not allow (for example `"frame_count": 50`, or an unsupported `model_profile`, size, `fps` or `enhance_prompt`) returns `unsupported_setting`. A `seed` outside 0–2147483647 or a `style` that is not one of the listed strings returns `validation_error`, because those limits do not depend on the profile.

**Malformed JSON bodies.** A JSON body that cannot be parsed returns `400` `invalid_request`. This includes a body nested too deeply, which returns "JSON parse error - the data is nested too deeply."

**Nothing is created.** A rejected request creates no job and stores no uploaded file. It does count toward the generation rate limit; see [Rate limits](#rate-limits).

## Check generation status

`GET /api/v1/generations/{job_id}/`

Processing response:

```json
{
  "job_id": "6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006",
  "mode": "text_to_video",
  "status": "processing",
  "progress": null,
  "prompt": "A slow cinematic shot of waves around a lighthouse at sunset",
  "settings": {
    "model_profile": "wan2.1-t2v-1.3b-480p",
    "width": 832,
    "height": 480,
    "frame_count": 81,
    "fps": 16,
    "seed": null,
    "style": "cinematic",
    "enhance_prompt": false
  },
  "has_input_image": false,
  "created_at": "2026-01-01T12:00:00Z",
  "started_at": "2026-01-01T12:01:00Z",
  "completed_at": null,
  "result": null,
  "error": null
}
```

`progress` stays `null` until a trustworthy progress signal exists; the API never invents percentage values. It is `100` only when the video is available.

Completed response (other fields as above):

```json
{
  "status": "completed",
  "progress": 100,
  "completed_at": "2026-01-01T12:08:00Z",
  "result": {
    "preview_url": "/api/v1/generations/6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006/video/",
    "download_url": "/api/v1/generations/6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006/video/?download=1",
    "download_filename": "geetha-6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006.mp4",
    "is_mock": false
  },
  "error": null
}
```

`result` fields:

| Field | Meaning |
|---|---|
| `preview_url` | Streams the MP4 inline for an HTML `<video>` element. |
| `download_url` | The same video sent as an attachment (`Content-Disposition: attachment`). |
| `download_filename` | The file name for the download: `geetha-<job_id>.mp4`, or `geetha-mock-<job_id>.mp4` when `is_mock` is `true`. It is exactly the name in the video's `Content-Disposition` header, so clients can use it for an `<a download>` attribute. |
| `is_mock` | `true` when the video came from the local mock service (`AI_SERVICE_MODE=mock`). Mock output is a synthetic FFmpeg test pattern, **not** an AI-generated video, and clients must label it as such. `false` means the video came from the remote AI service. It is derived from the stored AI-service job reference (mock references start with `mock-`), so it needs no extra database column. |

`result` is `null` for every status other than `completed`.

Failed response (other fields as above):

```json
{
  "status": "failed",
  "progress": null,
  "result": null,
  "error": {
    "code": "service_unavailable",
    "message": "The AI service is unavailable. Check that the Colab notebook is running.",
    "request_id": "b1946ac92492d2347c6235b4d2611184"
  }
}
```

`error.code` is a stable category for diagnostics; all codes are listed in [Job error codes](#job-error-codes). `error.message` is safe to show to users. `error.request_id` is the ID of the request that created the job.

### Job error codes

These are the codes a `failed` job can report in `error.code`, with the exact `error.message` the API returns. They appear only on jobs (`GET /api/v1/generations/{id}/` and the cancel response), never as the HTTP error of a request; request errors are listed under [Error shape](#error-shape). Technical details of each failure are only in the server log, under the job ID and request ID.

**Set by the backend (any mode)**

| Code | When | `error.message` |
|---|---|---|
| `cancelled` | The owner cancelled the job while it was `queued`. | You cancelled this video. |
| `queue_timeout` | The job was still `queued` after `GENERATION_QUEUE_TIMEOUT_SECONDS`; see [Queue timeout](#queue-timeout). | The video could not be started in time. Please try again. |
| `worker_interrupted` | The job had no heartbeat for 5 minutes (its worker crashed, was killed or lost power, hit an unexpected error, or could not record the result), or a separate worker was stopped with Ctrl+C while processing it. | Generation was interrupted. Please try again. |
| `unsupported_setting` | The job's model profile no longer exists or belongs to the other mode, or its stored size, frame count, fps or prompt enhancement is no longer allowed by the profile. Checked by the worker before any AI work. (The same code is also a `400` request error when a request asks for an unsupported setting.) | This model profile is no longer available. *or* This job's settings are no longer supported. Please submit a new request. |
| `invalid_job_settings` | The stored settings are missing, of the wrong type, or not an object, or the stored prompt is not text. | This job's stored settings are invalid. Please submit a new request. |
| `input_missing` | An image-to-video job has no stored image, its file is gone, or its reference does not lead to a regular file inside `MEDIA_ROOT`. | The uploaded image is no longer available. Please submit a new request. |
| `internal_error` | An unexpected error while preparing the job or while generating it. | An unexpected error occurred while preparing the video. *or* An unexpected error occurred while generating the video. |

**Set by the AI client, either mode**

| Code | When | `error.message` |
|---|---|---|
| `invalid_output` | The video is missing, smaller than 1 KB, or not an MP4 (no `ftyp` header). | The AI service returned an empty video. *or* The AI service returned a file that is not an MP4 video. |

**Mock mode only** (`AI_SERVICE_MODE=mock`)

| Code | When | `error.message` |
|---|---|---|
| `mock_unavailable` | FFmpeg was not found on `PATH` or through `imageio-ffmpeg`. | Mock mode needs FFmpeg on PATH (for example: winget install Gyan.FFmpeg). |
| `mock_failed` | FFmpeg failed or took longer than 120 seconds. | The mock video could not be rendered. |

**Remote mode only** (`AI_SERVICE_MODE=remote`)

| Code | When | `error.message` |
|---|---|---|
| `service_not_configured` | `AI_SERVICE_BASE_URL` or `AI_SERVICE_API_KEY` is empty. | The AI service is not configured. Set AI_SERVICE_BASE_URL and AI_SERVICE_API_KEY. |
| `service_unavailable` | The service could not be reached, or a reply could not be read. | The AI service is unavailable. Check that the Colab notebook is running. |
| `service_auth_failed` | The service answered HTTP `401` or `403`. | The AI service rejected the API key. |
| `service_rejected` | The service answered HTTP `400` or `422`. | The AI service rejected the request. |
| `service_error` | The service answered any other HTTP error status (for example `404`, `429` or `5xx`). | The AI service returned an error. |
| `invalid_response` | A reply was not a JSON object, was over 64 KiB, had an invalid job ID, or reported an unknown status. | The AI service sent an invalid response. *or* The AI service did not return a valid job ID. *or* The AI service reported an unknown status. |
| `service_timeout` | The overall deadline `AI_SERVICE_MAX_WAIT` passed during submission, polling or the download. | The video took too long to generate. |
| `download_failed` | The download stalled, dropped, or ended before its `Content-Length`. | The video could not be downloaded from the AI service. |
| `output_too_large` | The video is larger than `AI_SERVICE_MAX_VIDEO_BYTES`, by its `Content-Length` or by the bytes received. | The generated video is larger than this application accepts. |
| `generation_failed` | The service reported the job `failed` without a usable error code. | The model could not generate this video. |
| *the service's own code* | The service reported the job `failed` with an `error.code` of 1 to 50 lower-case letters, digits or `_`. That code is passed through (for example a hypothetical `out_of_memory`); the service's own message is not. | The model could not generate this video. |

Because of the last row, remote mode can report codes that are not in this table. Clients should show `error.message` and treat unknown codes as a generic failure. A status check that fails with `service_unavailable` or a `5xx` `service_error` is retried up to twice within the deadline before the job fails. No other step is retried, and a failed job is never resubmitted automatically.

## List generation history

`GET /api/v1/generations/?page=1&page_size=20&status=completed&mode=text_to_video`

- `page_size` defaults to 20, maximum 50.
  - A larger value is capped at 50.
  - A value that is not a positive whole number (`abc`, `0`, `-1`, empty) falls back to 20.
- `page` starts at 1. A page that does not exist, or a value that is not a positive whole number, returns `404` `not_found` with the message `Invalid page.`.
- `prompt_preview` is the stored prompt if it has at most 80 characters, otherwise its first 77 characters followed by `...`. It is empty for an image job without a prompt.
- `video_available` is `true` only for a `completed` job with a stored video.
- `status` and `mode` are optional filters; unknown values are ignored.
- Results are newest first.
- `next` and `previous` are absolute URLs (or `null`) that keep the other query parameters. They use the `Host` header of the request (checked against `DJANGO_ALLOWED_HOSTS`) and its scheme. Behind an HTTPS reverse proxy the scheme is `https` only when `DJANGO_BEHIND_HTTPS_PROXY` is on and the proxy sends `X-Forwarded-Proto: https`; see [Production deployment](../backend/README.md#production-deployment). `X-Forwarded-Host` is never used.

Response:

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "job_id": "6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006",
      "mode": "text_to_video",
      "prompt_preview": "A slow cinematic shot of waves around a lighthouse at sunset",
      "status": "completed",
      "created_at": "2026-01-01T12:00:00Z",
      "completed_at": "2026-01-01T12:08:00Z",
      "video_available": true
    }
  ]
}
```

## Cancel a queued job

`POST /api/v1/generations/{job_id}/cancel/` (no body; CSRF token required, like every `POST`)

Cancels one of the caller's own jobs while it is still `queued`. The job becomes `failed` with `error.code` `cancelled` and the message "You cancelled this video."; there is no separate `cancelled` status. Cancelling frees the owner's [one active job](#one-active-job-per-owner) at once. Nothing is deleted: an uploaded input image is kept, and a queued job has no video.

| Response | When |
|---|---|
| `200` with the [job object](#check-generation-status) | The job was cancelled, or had already been cancelled (repeating the request is harmless and changes nothing) |
| `409` `not_cancellable` | The job is `processing` ("This video is already being generated and can no longer be cancelled."), `completed` ("This video has already finished.") or `failed` for another reason ("This video has already stopped.") |
| `404` `not_found` | No such job, or it belongs to someone else |
| `403` `forbidden` | Missing or invalid CSRF token |
| `405` `method_not_allowed` | `GET` or `HEAD`, or another method sent with a valid CSRF token. `PUT`, `PATCH` and `DELETE` without a token get `403` first (the CSRF check runs before the method check). |

```json
{
  "error": {
    "code": "not_cancellable",
    "message": "This video is already being generated and can no longer be cancelled.",
    "request_id": "b1946ac92492d2347c6235b4d2611184"
  }
}
```

- **Racing a worker.** Cancellation is one conditional `UPDATE` (`WHERE status = 'queued'`). If a worker claims the job at the same moment, exactly one of them wins: either the job is cancelled and never starts, or it starts and the cancellation gets `409`.
- **Not rate limited.** Cancelling does not use the generation allowance.
- **Frontend.** A queued job's "Generating video" card has a **Cancel** button, shown only while the job is `queued`. Cancelling turns the card into "Video cancelled" in that job's own conversation and enables Send again. If the job started first, the page shows the `409` message and the button disappears.

## Retrieve/download a generated video

`GET /api/v1/generations/{job_id}/video/` (add `?download=1` to download)

Returns the MP4 (`Content-Type: video/mp4`) after the ownership check. It never exposes filesystem paths. These cases all return `404` with code `not_found`, with or without a `Range` header:

- a job without a verified output;
- a job whose file is missing;
- a job whose stored reference does not lead to a regular file inside `MEDIA_ROOT` (a directory, a path outside it, or a link to outside).

A file that exists but cannot be read returns `500` with code `internal_error`, and the message contains no path.

Every video response carries:

- `Accept-Ranges: bytes`;
- `Cache-Control: private, no-store` and `Vary: Cookie`, because the video is private to its owner (the `404` and `416` errors carry them too);
- `X-Content-Type-Options: nosniff`.

`Content-Disposition` is `inline` for playback, or `attachment` with exactly `?download=1` (any other value plays inline). Either way the file name is `geetha-<job_id>.mp4`, or `geetha-mock-<job_id>.mp4` for mock output, the same as `result.download_filename`. The name is built only from the job ID, never from the prompt or an uploaded file name.

**Byte ranges.** Browsers request ranges for `<video>` playback and seeking. A single range in the `bytes` unit is supported (`start` and `end` are inclusive byte offsets):

| `Range` header | Meaning |
|---|---|
| `bytes=0-99` | Bytes 0 to 99. An `end` beyond the file is shortened to the last byte. |
| `bytes=100-` | From byte 100 to the end. |
| `bytes=-500` | The last 500 bytes (the whole file if it is shorter). |

| Status | When | Body and headers |
|---|---|---|
| `200 OK` | No `Range` header, or one that is ignored (see below) | The whole MP4, with `Content-Length` |
| `206 Partial Content` | A satisfiable range | Only the requested bytes, with `Content-Range: bytes <start>-<end>/<size>` and `Content-Length` set to the range length |
| `416 Range Not Satisfiable` | A well-formed range that selects no bytes: it starts at or beyond the end of the file, it is `bytes=-0`, or the file is empty | `Content-Range: bytes */<size>`, and the standard error shape with code `range_not_satisfiable` |

- **Ignored ranges.** Multiple ranges (`bytes=0-99,200-299`), other units, malformed values (for example `bytes=5-1` or non-ASCII digits) and any request with `If-Range` get the whole file with `200`. These responses carry no `ETag` or `Last-Modified`, so `If-Range` can never be validated. Multipart (`multipart/byteranges`) responses are not supported.
- **Streaming.** Partial responses are streamed from the file in chunks of at most 64 KiB, so only the requested bytes are read. Ownership is checked before any range is read.
- **HEAD.** `HEAD` returns the same status and headers without a body.

## CSRF token

`GET /api/v1/csrf/`

```json
{"csrf_token": "<64-character token>"}
```

- Sets the `csrftoken` cookie and returns the matching token.
- Send the token in the `X-CSRFToken` header on every `POST` (see [Common conventions](#common-conventions)).
- It does not create a session; a guest's session starts with their first job.
- The response is private (`Cache-Control: private, no-store`).
- If a `POST` gets `403` with a message starting `CSRF Failed`, fetch a new token and retry once, as the frontend does.

## Model profiles

`GET /api/v1/profiles/`

Lists every model profile with its mode, `is_default` (whether it is the profile used for its mode when a request names none; exactly one per mode), allowed sizes, allowed frame counts, default frame count, fps, whether prompt enhancement is supported, notes, and `t4_verified` (whether the profile has actually been tested on the Colab T4; currently `false` for all profiles).

The frontend reads this list when the page loads. For each mode it uses the default profile: it disables Settings options that profile does not allow and sends the profile's name as `settings.model_profile`. If the list cannot be loaded, the page keeps its built-in options (832×480 or 480×832, 33/49/81 frames), which match the default profiles, and sends no `model_profile`.

Response (from the running server, 21 October 2026):

```json
{
  "results": [
    {
      "name": "wan2.1-t2v-1.3b-480p",
      "mode": "text_to_video",
      "is_default": true,
      "sizes": [{"width": 832, "height": 480}, {"width": 480, "height": 832}],
      "frame_counts": [17, 21, 25, 29, 33, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73, 77, 81],
      "default_frame_count": 81,
      "fps": 16,
      "supports_prompt_enhancement": true,
      "t4_verified": false,
      "notes": "Official 1.3B model; needs about 8 GB VRAM with model offloading."
    },
    {
      "name": "wan2.1-i2v-14b-480p",
      "mode": "image_to_video",
      "is_default": true,
      "sizes": [{"width": 832, "height": 480}, {"width": 480, "height": 832}],
      "frame_counts": [17, 21, 25, 29, 33, 37, 41, 45, 49, 53, 57, 61, 65, 69, 73, 77, 81],
      "default_frame_count": 81,
      "fps": 16,
      "supports_prompt_enhancement": false,
      "t4_verified": false,
      "notes": "Wan 2.1 image-to-video only ships as a 14B model; T4 feasibility is unproven."
    }
  ]
}
```

- **Not paginated.** `results` is the complete list; there is no `count`, `next` or `previous`.
- **`sizes`** are the allowed `width` × `height` pairs.
- **`frame_counts`** are every allowed `frame_count`.
- **`fps`** is the only accepted `fps`.
- **`notes`** is human-readable text and may change.

## Health

`GET /api/v1/health/` returns `{"status": "ok"}`. It checks only that Django is responding, not the database or the AI service. It creates no session, is not rate limited, and carries no `Cache-Control` header.

## Rate limits

Only the two generation endpoints are rate limited, and they share **one** allowance per identity:

- `POST /api/v1/generations/text-to-video/`
- `POST /api/v1/generations/image-to-video/`

| Limit | Value |
|---|---|
| Burst | 5 generation requests per minute |
| Sustained | 30 generation requests per hour |

Both limits apply at once; for example, 3 text-to-video and 2 image-to-video requests within a minute use up the burst allowance.

- **Identity.** Signed-in users are counted by user ID. Guests are counted by the direct client address (`REMOTE_ADDR`); `X-Forwarded-For` and similar headers are ignored, so they cannot be used to evade the limit, and clearing cookies does not reset it. Guests behind one shared address (for example a lab network or a single proxy/tunnel) share one allowance; deploying behind a proxy requires revisiting this rule.
- **What counts.** Only `POST` requests to the two generation endpoints count. The limit is checked after the CSRF check and before request validation. CSRF-rejected requests (`403`) do not count. Requests that fail validation (`400`) or are refused because a job is already active (`409`) do count. As in Django REST Framework's standard behaviour, a request rejected by the burst limit still counts toward the hourly limit.
- **Not limited.** `GET` endpoints are never rate limited: health, profiles, `csrf/`, history, status polling, and video playback or download. Other methods sent to the generation endpoints never count either. `GET`, `HEAD`, `PUT`, `PATCH` and `DELETE` always return `405` (`method_not_allowed`, with `Allow: POST, OPTIONS`), and `OPTIONS` returns `200`, even after the limit is reached. `PUT`, `PATCH` and `DELETE` still need a CSRF token first, so without one they return `403`.
- **Response.** A limited request creates no job and stores no upload. It returns `429` with code `rate_limited` and a `Retry-After` header (seconds):

```json
{
  "error": {
    "code": "rate_limited",
    "message": "Too many video requests. Please wait 42 seconds before trying again.",
    "request_id": "b1946ac92492d2347c6235b4d2611184"
  }
}
```

- **Development limitations.** Counters are kept in Django's default in-memory cache. They reset when the server restarts and are not shared between server processes, so a multi-process deployment needs a shared cache for reliable limits. These limits reduce accidental or casual overuse; they do not protect against distributed abuse or guarantee GPU capacity.

## Request and upload limits

Units: 1 MB = 1,048,576 bytes (MiB); 1 megapixel = 1,000,000 pixels.

| Limit | Value | Where it is enforced |
|---|---|---|
| Whole generation request (`POST` to `/api/v1/generations/...`) | 26 MB declared `Content-Length` | Before the body is read (`GenerationRequestSizeMiddleware`), so before CSRF and rate limiting |
| Uploaded image | 25 MB | Image validation (`400` `image_too_large`) |
| Image dimensions | at most 4096 × 4096 pixels and 16 megapixels | Image validation, from the file headers |
| JSON / form bodies and non-file multipart fields | 2.5 MB (Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` default) | Django request parsing |
| Form fields / files per request | 1000 / 100 (Django defaults) | Django request parsing |

- A request whose declared size exceeds 26 MB, or a JSON / form body over 2.5 MB, returns `413` with code `request_too_large`. It creates no job, stores no file, and does not use rate-limit allowance. Too many form fields or files returns `400` with code `invalid_request`.
- A malformed `Content-Length` header (anything other than plain digits) returns `400` `invalid_request`. A request without `Content-Length` is read as having an empty body, so it fails normal validation.
- Image codes: `image_too_large` (over 25 MB), `image_dimensions_too_large` (over 4096 pixels on a side or over 16 megapixels), `unsupported_image_type` (not PNG, JPEG, or WebP), `invalid_image` (empty, or headers damaged or incomplete).
- **Limitations.** Only the declared `Content-Length` is checked. Under WSGI Django never reads beyond it, which bounds what the application parses, but it does not stop a client from sending more data to the web server, and an ASGI deployment buffers the whole body before Django runs. A deployment must therefore also set an upload limit of 26 MB at the reverse proxy or hosting layer (for example `client_max_body_size 26m;` in nginx). Image checks read only the format headers; pixel data is not decoded, so an image with valid headers but corrupt data can still be accepted, and the AI service must handle such input safely.

## Error shape

```json
{
  "error": {
    "code": "unsupported_setting",
    "message": "Frame count must be between 17 and 81 and of the form 4n + 1.",
    "request_id": "b1946ac92492d2347c6235b4d2611184",
    "fields": {
      "settings": {
        "frame_count": ["Frame count must be between 17 and 81 and of the form 4n + 1."]
      }
    }
  }
}
```

`fields` appears only on validation errors (`400`) and mirrors the request structure. `message` is the first field error. Generic validation problems such as a missing, blank, or wrongly typed field use code `validation_error`; other codes include `unknown_field` (see [Request field rules](#request-field-rules)), `unsupported_setting`, `image_too_large`, `image_dimensions_too_large`, `unsupported_image_type`, `invalid_image`, `invalid_request`, `forbidden`, `not_found`, `method_not_allowed`, `unsupported_media_type`, `request_too_large`, `range_not_satisfiable`, `rate_limited`, and `internal_error`. Failed jobs report a stable `error.code` and a user-safe `error.message` (see [Job error codes](#job-error-codes)); when the remote AI service fails, its own message is written to the server log and users see a fixed message instead.
