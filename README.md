# Geetha — Controllable AI Video Generation from Text and Image Prompts

## Overview

This project is a web application for generating short videos from text descriptions or uploaded images. Users configure supported generation settings, submit a generation job, monitor its progress, preview and download the result, and review previous generations.

The web application itself is implemented and runs end to end: the browser interface, the Django REST API, MySQL job storage, and a background generation worker. **The AI model is not connected yet.** Videos are currently produced by a mock generator that renders an FFmpeg test pattern. Real generation with Wan 2.1 on Google Colab is the main unfinished part.

The architecture separates the browser interface, the Django REST API, MySQL metadata storage, and a GPU inference service that will run separately in Google Colab. This separation is intentional: Colab and the Django host do not share a filesystem, and model weights must remain outside source control.

## Quick start

The full setup is in [backend/README.md](backend/README.md#installation):
1. Create a virtual environment and `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env`.
3. Create the MySQL database and run `python manage.py migrate`.
4. Run `python manage.py runserver` from `backend/`.
5. Open <http://127.0.0.1:8000/>.

The page must be opened through Django; see [frontend/README.md](frontend/README.md). The handoff summary, with what works, what does not, and the known limitations, is [docs/handoff.md](docs/handoff.md).

## Features and status

Implemented and tested in mock mode:

- Text-to-video and image-to-video job requests, with validated prompts, images and settings.
- Job progress and failure status, with stable error codes and user-safe messages.
- Queued-job cancellation, queue timeout, one active job per owner, and recovery of interrupted jobs.
- Video preview and owner-only download.
- Generation history backed by metadata in MySQL.
- The Django side of protected communication with the Colab inference service (an authenticated HTTP client with timeouts and download limits), tested against a local fake service only.

Not implemented yet:

- The Colab inference service itself (`ai_service/colab/` is still empty).
- Real text-to-video and image-to-video generation with Wan 2.1. No model profile has been verified on the T4.
- Optional prompt enhancement with a compatible Qwen3 checkpoint.

## Technology stack

- Frontend: HTML5, CSS3, and browser JavaScript.
- Main backend: Python, Django, and Django REST Framework.
- Database: MySQL for job metadata, settings, status, file references, sessions and Django's standard user tables.
- Inference (planned): Wan 2.1 and optionally Qwen3, executed with PyTorch and compatible supporting libraries.
- Media processing: FFmpeg for mock videos (from `PATH` or the `imageio-ffmpeg` package). Uploaded images are checked from their file headers without an imaging library; the Colab service may need more.
- GPU environment (planned): Google Colab with an NVIDIA T4, using existing model files in Google Drive.

The exact Wan 2.1 implementation, checkpoint variant, and compatible Qwen3 checkpoint will be confirmed during the feasibility phase. No checkpoint details are assumed here.

## High-level architecture

```text
Browser frontend
      |
      | REST API calls (JSON or multipart), status polling
      v
Django + Django REST Framework ------> MySQL (jobs, sessions)
      |                                  ^
      | uploads, owner-only video        | claim job, heartbeat, record result
      v                                  |
media/inputs, media/outputs <------- Generation worker
                                         |
                                         | AI client (AI_SERVICE_MODE)
                                         v
                    mock (default): FFmpeg test video
                    remote: Google Colab AI service (not built yet)
                                         |
                                         v
                              Wan 2.1 / optional Qwen3 (planned)
```

Images and videos are not stored in MySQL. The database stores file names and job metadata, and the files live under `media/`. In remote mode, the client uploads the input image to the AI service with the job and downloads the finished video back.

## Directory guide

- `frontend/`: static browser pages, styles, scripts, and user-facing assets.
- `backend/`: the Django project. `config/` holds settings and URLs; `apps/generator/` holds the job model, its migration, the REST API, validation and the generation worker; `tests/` holds the automated backend tests. See [backend/README.md](backend/README.md).
- `ai_service/`: the AI clients used by the worker (`client/mock.py` and `client/remote.py`). It is not the Colab notebook itself; `ai_service/colab/` is still empty.
- `database/`: early design notes. The implemented schema is the `generation_jobs` table, defined by the Django model and migration in `backend/apps/generator/` and described in [docs/database-design.md](docs/database-design.md).
- `media/`: local development locations for inputs and outputs; generated media is ignored by Git.
- `tests/`: placeholder folders for future cross-component integration and acceptance tests, currently empty. The automated tests are in `backend/tests/`.
- `docs/`: requirements, architecture, the implemented API reference, database design, security guidance, testing strategy, and roadmap with current status.

## Current implementation status

The phase-by-phase status is in [docs/development-roadmap.md](docs/development-roadmap.md). In short:

- **Done:** project structure and requirements, the frontend, the Django API, MySQL with migrations, and generation job management. All of these run against the mock AI client.
- **Not started:** AI checkpoint inspection and T4 feasibility, the Colab inference service, and real text-to-video and image-to-video integration.
- **Partly done:**
  - history and reliability: retention is decided for the demo (keep everything; manual cleanup only), but there is no automatic job retry;
  - end-to-end tests: the backend tests cover mock mode and a fake remote service; the current frontend still needs a browser check;
  - documentation: the backend and API are documented and handed off (see [docs/handoff.md](docs/handoff.md)); the demonstration is not done.

The next technical phase inspects the existing AI checkpoints and tests model feasibility on the target T4 environment.

## Important constraints

- Do not commit secrets, local `.env` files, virtual environments, caches, model weights, or generated media.
- Do not assume Colab and Django share a filesystem.
- Do not claim a model variant is feasible on a T4 until it has been tested.
- Keep Whisper speech-to-text outside the initial scope.
