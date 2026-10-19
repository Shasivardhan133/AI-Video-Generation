# Controllable AI Video Generation from Text and Image Prompts

## Overview

This project will become a web application for generating short videos from text descriptions or uploaded images. Users will be able to configure supported generation settings, submit a generation job, monitor its progress, preview and download the result, and review previous generations.

The initial architecture separates the browser interface, Django REST API, MySQL metadata storage, and GPU-based inference service running separately in Google Colab. This separation is intentional: Colab and the Django host do not share a filesystem, and model weights must remain outside source control.

## Planned features

- Text-to-video generation using a feasible Wan 2.1 configuration.
- Image-to-video generation using a supported Wan 2.1 configuration.
- Optional prompt understanding or enhancement with a compatible Qwen3 checkpoint.
- Configurable, validated generation settings.
- Job progress and failure status.
- Video preview and controlled download.
- Generation history backed by metadata in MySQL.
- Protected communication between Django and the Colab inference service.

These features are planned; no application endpoints or inference workflow are implemented yet.

## Technology stack

- Frontend: HTML5, CSS3, and browser JavaScript.
- Main backend: Python, Django, and Django REST Framework.
- Database: MySQL for users, job metadata, settings, status, and file references.
- Inference: Wan 2.1 and optionally Qwen3, executed with PyTorch and compatible supporting libraries.
- Media processing: Pillow and FFmpeg where required.
- GPU environment: Google Colab with an NVIDIA T4, using existing model files in Google Drive.

The exact Wan 2.1 implementation, checkpoint variant, and compatible Qwen3 checkpoint will be confirmed during the feasibility phase. No checkpoint details are assumed here.

## High-level architecture

```text
Browser frontend
      |
      | proposed REST API calls
      v
Django + Django REST Framework
      |                  \
      | MySQL metadata     \ protected inference API
      v                    v
    MySQL             Google Colab AI service
                              |
                              v
                       Wan 2.1 / optional Qwen3
                              |
                              v
                    transferable output file/reference
```

Large input and output videos will not be stored directly in MySQL. The application will store references and lifecycle metadata, while the eventual transfer/storage mechanism will be selected after the Colab feasibility and deployment constraints are known.

## Directory guide

- `frontend/`: static browser pages, styles, scripts, and user-facing assets.
- `backend/`: the future Django project and its domain applications. Django scaffolding is intentionally deferred.
- `ai_service/`: client contracts and schemas used by Django to communicate with the separate Colab service; it is not the Colab notebook itself.
- `database/`: database design notes and future schema-related artifacts; no tables or migrations are created in this phase.
- `media/`: local development locations for inputs and outputs; generated media is ignored by Git.
- `tests/`: cross-component integration and acceptance tests.
- `docs/`: requirements, architecture, API proposals, database design, security guidance, testing strategy, and roadmap.

## Development phases

The staged plan is documented in [docs/development-roadmap.md](docs/development-roadmap.md). The current phase establishes project organization and requirements only. The next technical phase inspects existing AI checkpoints and tests model feasibility on the target T4 environment.

## Current implementation status

**Phase 1 - project structure and requirements: in progress/completed as documentation foundation.**

- Directory boundaries and architectural responsibilities are documented.
- Proposed API contracts are documented but are not implemented.
- Django project scaffolding, migrations, database tables, frontend behavior, and AI inference are intentionally deferred.
- No model weights, generated media, or dependencies were downloaded or installed.

## Important constraints

- Do not commit secrets, local `.env` files, virtual environments, caches, model weights, or generated media.
- Do not assume Colab and Django share a filesystem.
- Do not claim a model variant is feasible on a T4 until it has been tested.
- Keep Whisper speech-to-text outside the initial scope.

