# Proposed API contracts

**Status: proposed, not implemented.** Paths, fields, and response details may be refined during Django initialization. These examples are contracts to guide later implementation, not claims that endpoints currently work.

## Common conventions

- Base path: `/api/v1/` (proposed).
- JSON responses use `Content-Type: application/json`.
- Image submission uses `multipart/form-data`.
- Job identifiers are opaque UUIDs.
- A successful creation returns `202 Accepted` because generation is asynchronous.
- Validation errors use `400 Bad Request`; missing authentication uses `401`; forbidden ownership access uses `403`; missing jobs use `404`; unavailable dependencies use a safe `502`/`503` response.

## Create a text-to-video job

`POST /api/v1/generations/text-to-video/`

```json
{
  "prompt": "A slow cinematic shot of waves around a lighthouse at sunset",
  "settings": {
    "model_profile": "wan-tested-profile",
    "width": 512,
    "height": 512,
    "frame_count": 49,
    "fps": 16,
    "seed": 12345
  }
}
```

Proposed response (`202 Accepted`):

```json
{
  "job_id": "6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006",
  "mode": "text_to_video",
  "status": "accepted",
  "created_at": "2026-01-01T12:00:00Z",
  "status_url": "/api/v1/generations/6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006/"
}
```

## Create an image-to-video job

`POST /api/v1/generations/image-to-video/`

Proposed multipart fields:

- `image`: required image file.
- `prompt`: optional supporting prompt, if supported by the tested model profile.
- `settings`: JSON-encoded validated settings.

Proposed response (`202 Accepted`) has the same job envelope, with `"mode": "image_to_video"`.

## Check generation status

`GET /api/v1/generations/{job_id}/`

Proposed processing response:

```json
{
  "job_id": "6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006",
  "mode": "text_to_video",
  "status": "processing",
  "progress": null,
  "created_at": "2026-01-01T12:00:00Z",
  "started_at": "2026-01-01T12:01:00Z",
  "completed_at": null,
  "result": null,
  "error": null
}
```

The progress field should remain `null` until a trustworthy progress signal exists; the API must not invent percentage values.

Proposed completed response:

```json
{
  "job_id": "6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006",
  "mode": "text_to_video",
  "status": "completed",
  "progress": 100,
  "created_at": "2026-01-01T12:00:00Z",
  "started_at": "2026-01-01T12:01:00Z",
  "completed_at": "2026-01-01T12:08:00Z",
  "result": {
    "preview_url": "/api/v1/generations/6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006/video/",
    "download_url": "/api/v1/generations/6b7d9b9a-4b0c-4f88-8c34-8f3d7e76f006/video/"
  },
  "error": null
}
```

## List generation history

`GET /api/v1/generations/?page=1&page_size=20&status=completed`

Proposed response:

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

`GET /api/v1/generations/{job_id}/video/`

The endpoint should enforce ownership/access policy and return a controlled stream or redirect to a short-lived authorized URL. It must not expose filesystem paths. A job without a verified output returns `404` or a documented not-ready response rather than a fake file.

## Proposed error shape

```json
{
  "error": {
    "code": "unsupported_setting",
    "message": "The selected frame count is not supported by this model profile.",
    "request_id": "request-correlation-id"
  }
}
```

