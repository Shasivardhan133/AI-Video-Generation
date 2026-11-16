# Frontend

The browser interface: prompt entry, image upload and preview, generation settings, job status, video playback and download, and history.

- `pages/index.html`: the page.
- `css/styles.css`: styles.
- `js/main.js`: all behaviour, including the API adapter for the Django backend.

## Open it through Django

Start the backend (see [backend/README.md](../backend/README.md)) and open <http://127.0.0.1:8000/>. Django serves this folder in development (`config/urls.py`): `/` is `pages/index.html`, and `/css/`, `/js/` and `/assets/` are served from here.

Do not open `index.html` directly (`file://`) or from a separate static server (for example VS Code Live Server). The script calls the API with the relative path `/api/v1/`, so the page must come from the same origin as the API. Its session cookie (guest job ownership) and CSRF token only work same-origin. No CORS is configured, deliberately.

## How it talks to the API

- **CSRF.** Before any `POST`, the script fetches `GET /api/v1/csrf/` and sends the token in the `X-CSRFToken` header.
- **Requests.** All requests are same-origin (`credentials: "same-origin"`).
- **Jobs.**
  - Text-to-video posts JSON `{prompt, settings}`.
  - Image-to-video posts multipart (`image`, `prompt`, `settings` as JSON text).
  - The page then polls `GET /api/v1/generations/<id>/` every 3 seconds until the job is `completed` or `failed`.
- **Videos.** Playback and download use the `preview_url`, `download_url` and `download_filename` the API returns. The browser sends Range requests itself.
- **Settings.** The offered settings are limited to what `GET /api/v1/profiles/` reports.
- **Mock output.** Mock output (`result.is_mock`) is labelled "Mock test video · not AI-generated".
