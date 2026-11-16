# Security guidance

## Environment variables and secrets

Keep Django secrets, database passwords, service API keys, and tunnel credentials in environment variables or a local secret manager. `.env` files are ignored by Git; `.env.example` contains names and safe placeholders only. Never place model access tokens or credentials in frontend JavaScript, notebooks committed to the repository, logs, or documentation.

Django secret key (implemented in `backend/config/settings.py`, `resolve_secret_key`): when `DJANGO_DEBUG` is off, Django refuses to start with an `ImproperlyConfigured` error if `DJANGO_SECRET_KEY` is missing, blank, or one of the two published placeholders: `development-only-key-replace-before-deployment` (the built-in development key) or `replace-with-a-local-secret` (the value in `.env.example`). Surrounding spaces don't get a placeholder past this check. The error names the setting but never its value. A real key is used exactly as configured. Only with `DJANGO_DEBUG=True` does a missing or blank key fall back to the development key, and only then are the placeholders accepted, so DEBUG mode must never be used for a deployment. A value set in the process environment (even a blank one) takes precedence over `.env`.

## Input validation

Validate mode, prompt length, settings, numeric ranges, and model-profile capabilities on the Django side. Treat client-side validation as a usability feature, not a security control. Normalize data before sending it to the AI service and reject unknown or unsupported parameters.

## Uploaded-file validation

Validate declared and detected content type, extension, file signature, size, dimensions, and image decodability. Use generated server-side names rather than trusting user filenames. Store uploads outside executable/static paths and consider re-encoding images with Pillow before inference. Do not pass untrusted filenames or paths to shell commands.

Implemented (`apps/generator/validators.py`, `filenames.py`, `media_files.py`):
- **Content decides, not the name.** The declared content type and the extension are ignored. The format comes from the file signature (PNG, JPEG or WebP only). Its header is then parsed for structure and dimensions: at most 25 MB, 4096 px per side and 16 megapixels.
- **Not fully decoded.** Pixel data is never decoded, and images are not re-encoded (Pillow is not used). A file whose headers are valid but whose image data is corrupt can still pass; the AI service must handle that safely.
- **Stored names.** Files are stored as `inputs/YYYY/MM/<prompt keyword>-<uuid4 hex>.<extension from the detected format>`. The keyword keeps ASCII letters and digits only, and the client's file name and path are never used. Names are never reused, so an existing file is never overwritten.
- **No leftovers from failed requests.**
  - A rejected upload stores nothing.
  - If the image write fails part-way (for example, the disk is full), the partial file is deleted.
  - If the job row cannot be inserted, the stored image is deleted, but only after a fresh read confirms that no job refers to it. If that read fails, the file is kept and the media audit reports it.
  - Django's temporary upload files (used for uploads over 11 MB) are removed when the request ends, whether it was accepted or rejected.
- **Uploads are never served.** No URL serves `MEDIA_ROOT`; the API only reports `has_input_image`.

## File-size restrictions

Define separate limits for request body, image input, generated output, and history/download operations. Enforce limits at the web server and application layers. Reject oversized data before transferring it to Colab, and clean up temporary files after success or failure.

## API authentication between Django and AI service

Use a dedicated service credential transmitted only over an encrypted connection. Validate the service response, bind it to the expected job/correlation identifier, and rotate credentials. Do not expose the service credential to browsers. Add replay protection or short-lived signed requests if the final deployment requires it.

Implemented in `ai_service/client/remote.py`: the service is treated as untrusted input. Its job IDs must match a fixed format (1 to 64 ASCII letters, digits, `_` or `-`, not starting with `mock-`) before they are stored or put into a URL, so a reply cannot redirect the client's authenticated requests to other paths; IDs are also URL-quoted. JSON replies are limited to 64 KiB, videos to `AI_SERVICE_MAX_VIDEO_BYTES` (counted while streaming, not taken from `Content-Length`), and the whole generation to the `AI_SERVICE_MAX_WAIT` deadline, so a slow or hostile service cannot fill the disk or hold a worker indefinitely. Users see only fixed messages; logs keep truncated summaries and never contain the API key or a rejected job ID.

## Temporary tunnel risks

Colab tunnels can expose a changing public endpoint and may be reachable by unintended parties. Treat a tunnel as development-only: use strong authentication, narrow allowed operations, short-lived credentials, request limits, and no sensitive diagnostic responses. Never rely on obscurity of a tunnel URL.

## Generation capacity per owner

The frontend's one-video-at-a-time behaviour is a convenience, not a control. The API itself allows each owner (signed-in user or guest session) only one queued or processing job; further creation requests get `409 generation_in_progress` and store nothing (`apps/generator/active_jobs.py`). The check and the insert are serialized per owner with a MySQL named lock, so simultaneous requests cannot slip past it. Lock names are hashed, so session keys never appear in MySQL's lock tables. Together with the rate limit this bounds the GPU work one owner can queue. It does not stop a client that discards its session cookie from starting a new guest session for every request; only the per-address rate limit bounds that.

## Job cancellation and expiry

Only a job's owner can cancel it, through the same ownership check as the read endpoints (someone else's job returns `404`), and only while it is `queued`. Cancellation needs a CSRF token like every `POST`. Neither cancellation nor queue expiry (`queue_timeout`) deletes anything: input images and any other media stay, and the job record keeps its error code for diagnostics.

## Output access controls

Check the requesting user's ownership or an explicit sharing policy before returning a video. Do not expose local filesystem paths. Prefer short-lived signed media URLs or an authenticated streaming endpoint, and remove expired outputs according to a documented retention policy.

Implemented (`apps/generator/views.py` `JobVideoView`, `media_files.py`):
- **Owner only.** Videos are streamed only by `GET /api/v1/generations/<id>/video/`, after the ownership check: the signed-in user, or the guest's current session. Someone else's job, an unknown job, and a job whose guest session has expired or been deleted all return the same `404`.
- **Only real media files are read.** A stored reference is used only if it resolves to a regular file inside `MEDIA_ROOT`. Traversal names (`../x`), absolute paths, directories, and symlinks or junctions whose target lies outside `MEDIA_ROOT` count as missing:
  - the video endpoint returns `404 not_found`;
  - the worker fails an image job with `input_missing` and never sends such a file to the AI service.
  Links that stay inside `MEDIA_ROOT`, or a `MEDIA_ROOT` that is itself reached through a link, still work.
- **Unreadable files.** A file that exists but cannot be read (for example, a permissions error) gives `500 internal_error`. The message contains no path, and the details go to the server log.
- **Known limitation.** The check happens just before the file is opened. Someone who can already write to `MEDIA_ROOT` could swap the file for a link in between. Keep `MEDIA_ROOT` writable only by the application account.
- **Videos.** Generated videos are written to a temporary folder outside `MEDIA_ROOT` and verified (MP4 header, size cap on the remote client) before they are saved under a new name. A write that fails part-way is deleted; existing files are never overwritten. A failed job never refers to an output, so a partial video is never offered as a result.

Retention policy (decided for development and the final-year demo):
- **Kept indefinitely.** Uploaded images and generated videos are not deleted automatically: no scheduled cleanup, and Django does not delete files with their rows. Deleting a job or a user leaves its files in `media/`.
- **Read-only report.** `python manage.py find_orphaned_media` reports files that no record refers to. It is read-only and does not follow links out of `MEDIA_ROOT`. It masks prompt keywords in input file names and never prints prompts, owners or session keys. It refuses to run when `MEDIA_ROOT` is a drive or file-system root, or contains the application's code or frontend: there, source files would be listed as orphan candidates.
- **Manual deletion only.** Any deletion is manual, after someone has reviewed that report.
- **Revisit before public deployment.** Keeping private uploads indefinitely is acceptable for a local demo, not for a public service. Before public deployment, define time limits for inputs, outputs and failed jobs, and a cleanup process.

## Response caching

Responses that contain one user's data (job details, history pages, videos, CSRF tokens, and their error responses, including errors answered before any view runs, such as an oversized request or an unknown `/api/` path) are sent with `Cache-Control: private, no-store` and `Vary: Cookie` (implemented in `apps/generator/views.py`, `PrivateResponseMixin`). Shared caches such as proxies and CDNs, and the browser cache, must not keep a copy that a different user could receive.

## Reverse proxy headers

Forwarded headers are client-controlled unless a trusted proxy replaces them. `X-Forwarded-Proto` is honoured only when `DJANGO_BEHIND_HTTPS_PROXY=True`, which must be set only when Django is reachable solely through an HTTPS proxy that overwrites the header on every request. `X-Forwarded-Host` is never trusted, and the rate limit ignores `X-Forwarded-For`. Requirements are listed in `backend/README.md` (Production deployment).

## Safe error reporting

Return concise, actionable user messages. Keep stack traces, service tokens, raw prompts where sensitive, internal paths, and provider credentials out of responses. Correlate user-safe errors with server-side logs using a request/job identifier, while applying retention and access controls to those logs.

