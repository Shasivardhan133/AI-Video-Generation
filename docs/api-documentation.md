# API contracts

**Status: implemented in `backend/apps/generator/` (development).** The examples below match the current Django REST Framework implementation. Authentication, retention, and production storage are still open decisions.

## Common conventions

- Base path: `/api/v1/`.
- JSON responses use `Content-Type: application/json`.
- Image submission uses `multipart/form-data`.
- Job identifiers are opaque UUIDs.
- A successful creation returns `202 Accepted` because generation is asynchronous.
- Every response carries an `X-Request-ID` header. A client may send its own `X-Request-ID` (1–64 characters from `A-Z a-z 0-9 . _ -`); otherwise the server generates one.
- Ownership: signed-in users see only their own jobs. Anonymous users own jobs through their browser session cookie. A job owned by someone else returns `404`, exactly like a missing job, so job IDs cannot be probed.
- Status codes: validation errors use `400`; missing jobs, other users' jobs, and videos that are not ready use `404`; unsupported methods use `405`.

## Job status values

| Status | Meaning |
|---|---|
| `queued` | Accepted and stored; waiting for the worker. This is the status returned on creation. |
| `processing` | The worker has claimed the job and sent it to the AI service. `started_at` is set. |
| `completed` | A verified MP4 output is stored. `completed_at` is set and `result` is present. |
| `failed` | Generation stopped. `completed_at` is set and `error` explains why. |

Allowed transitions: `queued → processing → completed`, and `queued → failed` or `processing → failed`. `completed` and `failed` are final. There is no `accepted` or `cancelled` status; cancellation is not implemented. If the worker restarts while a job is `processing`, that job is marked `failed` with code `worker_interrupted`.

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

`settings` and every field inside it are optional; omitted values use the model profile's defaults.

| Setting | Allowed values (profile `wan2.1-t2v-1.3b-480p`) |
|---|---|
| `model_profile` | `wan2.1-t2v-1.3b-480p` (default for text to video) |
| `width` × `height` | `832 × 480` (default) or `480 × 832` |
| `frame_count` | 17–81 and of the form 4n + 1 (for example 33, 49, 81); default 81 |
| `fps` | `16` only |
| `seed` | integer 0–2147483647, or `null` |
| `style` | `cinematic`, `realistic`, `anime`, `3d_animation`, or `null` |
| `enhance_prompt` | `true` or `false` (default `false`) |

`prompt` is required for text to video and is limited to 1000 characters.

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

- `image`: required PNG, JPEG, or WebP file, at most 10 MB. The type is checked from the file contents, not the file name.
- `prompt`: optional supporting prompt, at most 1000 characters.
- `settings`: optional JSON-encoded settings string, validated against profile `wan2.1-i2v-14b-480p` (sizes `832 × 480` or `480 × 832`; prompt enhancement not available).

The response (`202 Accepted`) has the same shape as above, with `"mode": "image_to_video"`.

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

`error.code` is a stable category for diagnostics (for example `service_unavailable`, `service_timeout`, `invalid_output`, `worker_interrupted`, `internal_error`). `error.message` is safe to show to users. `error.request_id` is the ID of the request that created the job.

## List generation history

`GET /api/v1/generations/?page=1&page_size=20&status=completed&mode=text_to_video`

- `page_size` defaults to 20, maximum 50.
- `status` and `mode` are optional filters; unknown values are ignored.
- Results are newest first.

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
      "prompt_preview": "A slow cinematic shot of waves...",
      "status": "completed",
      "created_at": "2026-01-01T12:00:00Z",
      "completed_at": "2026-01-01T12:08:00Z",
      "video_available": true
    }
  ]
}
```

## Retrieve/download a generated video

`GET /api/v1/generations/{job_id}/video/` (add `?download=1` to download)

Returns the MP4 (`Content-Type: video/mp4`) after the ownership check. It never exposes filesystem paths. A job without a verified output, or whose file is missing, returns `404` with code `not_found`. Range requests (seeking within a partially downloaded video) are not supported by the development server.

## Model profiles

`GET /api/v1/profiles/`

Lists every model profile with its mode, allowed sizes, allowed frame counts, default frame count, fps, whether prompt enhancement is supported, notes, and `t4_verified` (whether the profile has actually been tested on the Colab T4; currently `false` for all profiles).

## Health

`GET /api/v1/health/` returns `{"status": "ok"}`. It checks only that Django is responding, not the database or the AI service.

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

`fields` appears only on validation errors (`400`) and mirrors the request structure. `message` is the first field error. Generic validation problems such as a missing or blank field use code `validation_error`; other codes include `unsupported_setting`, `image_too_large`, `unsupported_image_type`, `invalid_image`, `not_found`, and `method_not_allowed`.
