# Development roadmap

## Current status (21 October 2026)

The web application runs end to end in **mock mode**: browser, Django REST API, MySQL, generation worker and video playback all work, but videos are FFmpeg test patterns, not AI output. **No AI integration has been done:** the Colab service is not built, and no Wan 2.1 profile has been run on the T4.

| Phase | Status | Evidence |
|---|---|---|
| 1. Structure and requirements | Done | `docs/` and the project layout. |
| 2. AI checkpoints and T4 feasibility | **Not started** | Both model profiles have `t4_verified = False` (`backend/apps/generator/profiles.py`). |
| 3. Frontend | Done, against the real API in mock mode | `frontend/`: chat-style composer, conversations, job status, playback, download, history. |
| 4. Django and API foundations | Done | `backend/`; the API is described in `docs/api-documentation.md`. |
| 5. MySQL and migrations | Done | `GenerationJob` and migration `0001_initial`; see `docs/database-design.md`. |
| 6. Generation job management | Done | Worker with leases, recovery, queue timeout, cancellation, one active job per owner, guarded state changes (`backend/apps/generator/worker.py`, `models.py`). |
| 7. Protected AI service in Colab | **Client only** | `ai_service/client/remote.py` implements the client side of the contract and is tested against a local fake service. `ai_service/colab/` exists but is empty. Proposed design, awaiting approval: [ai-integration-plan.md](ai-integration-plan.md). |
| 8. Text-to-video integration | **Not started** | Needs phases 2 and 7. |
| 9. Image-to-video integration | **Not started** | Needs phases 2 and 7; the only Wan 2.1 image-to-video model is 14B, and its T4 feasibility is unproven. |
| 10. History, output handling, reliability | Partly done | **Done:** history, owner-only preview and download with byte ranges, request IDs in logs and error responses, a read-only orphaned-media report, and the demo retention policy (keep everything; manual review only). **Not done:** automatic job retry (it needs an idempotent AI-service contract), and a retention policy for public deployment. |
| 11. End-to-end tests | Partly done | **Done:** 587 backend tests in mock mode and against a fake remote service. **Not done:** earlier browser scenarios ran from a local scratch harness that is not in this repository, and the current frontend has not been re-verified in a browser; there is no end-to-end test with a real AI service. |
| 12. Documentation and demonstration | In progress | **Done:** backend documentation and handoff ([handoff.md](handoff.md)). **Not done:** the demonstration, and documentation of a tested model. |

The backend roadmap sections 3.1–3.9 are complete in mock mode:
- 3.1 audit and foundation;
- 3.2 configuration;
- 3.3 validation and security;
- 3.4 REST API;
- 3.5 job management;
- 3.6 database;
- 3.7 media and files;
- 3.8 testing;
- 3.9 documentation and handoff.

Their open items are listed in [handoff.md](handoff.md).

## Phases

1. **Project structure and requirements**  
   Establish directory boundaries, requirements, architecture, security assumptions, and proposed API/database contracts.
2. **Inspect existing AI checkpoints and test model feasibility**  
   Identify the actual Wan 2.1 implementation/checkpoint files available in Google Drive, verify compatibility, and measure T4 memory/runtime constraints. Do not duplicate weights.
3. **Build and test frontend**  
   Implement the static pages, styles, browser validation, upload preview, state views, and API adapter against an agreed contract or mock boundary.
4. **Initialize Django and implement API foundations**  
   Generate Django scaffolding, configure DRF, establish environment configuration, request IDs, health checks, and validation patterns.
5. **Configure MySQL and migrations**  
   Define Django models from the approved design, configure MySQL, and test migrations. Do not use MySQL as video storage.
6. **Implement generation job management**  
   Create durable job creation, lifecycle transitions, ownership checks, timestamps, and safe error handling.
7. **Expose a protected AI service in Colab**  
   Define and test the authenticated service contract, input/output transfer, timeouts, and cleanup under Colab constraints.
8. **Integrate text-to-video generation**  
   Connect the tested Wan 2.1 profile for text prompts and verify real outputs, resource usage, and failure cases.
9. **Integrate image-to-video generation**  
   Add validated image transfer and generation using a model profile proven to support the workflow.
10. **Add history, output handling, and reliability**  
    Implement history queries, authorized preview/download, retention, retries where safe, and observability.
11. **Run end-to-end tests**  
    Test success and failure paths from browser through Django, MySQL, Colab, and output access.
12. **Prepare project documentation and demonstration**  
    Record setup steps, tested model limitations, security decisions, test evidence, known issues, and a reproducible demonstration.

Whisper speech-to-text is outside the initial roadmap and may be evaluated as a later extension after the core video workflow is reliable.
