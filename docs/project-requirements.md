# Project requirements

## Problem statement

Video generation tools can be difficult to control, difficult to monitor, and difficult to integrate into a reproducible application workflow. This project will provide a focused web interface that accepts text or an image, exposes only supported generation controls, tracks asynchronous jobs, and makes completed outputs discoverable through history.

## Objectives

1. Provide a clear browser workflow for text-to-video and image-to-video requests.
2. Validate inputs and settings before a job reaches the GPU service.
3. Track each generation from submission through completion or failure.
4. Connect a Django API to a separately running, protected Google Colab inference service.
5. Preserve useful metadata without storing large video binaries in MySQL.
6. Evaluate Wan 2.1 feasibility on the available NVIDIA T4 before committing to a model configuration.
7. Produce a testable, documented foundation suitable for a final-year demonstration.

## Functional requirements

### User input and job creation

- The system shall accept a text prompt for text-to-video generation.
- The system shall accept a validated image upload for image-to-video generation.
- The system shall allow only generation settings supported by the selected model configuration.
- The system shall reject missing, malformed, oversized, or unsupported inputs with safe validation errors.
- The system shall return a unique job identifier when a request is accepted.

### Job processing

- The system shall represent queued, processing, completed, and failed states.
- The system shall expose status and timestamps for a submitted job.
- The system shall not report completion until the output is available and its reference has been recorded.
- The system shall retain a user-safe error message and a separate diagnostic correlation reference where appropriate.

### Results and history

- A permitted user shall be able to preview an available generated video.
- A permitted user shall be able to download an available generated video.
- A permitted user shall be able to list their generation history with useful metadata.
- History shall distinguish text-to-video and image-to-video jobs.

### Service integration

- Django shall communicate with the separate AI service through an authenticated contract.
- The design shall not rely on a shared local filesystem between Django and Colab.
- The system shall handle service timeouts, unavailable services, and invalid service responses explicitly.

## Non-functional requirements

- **Maintainability:** responsibilities remain separated between frontend, Django, database, and AI service.
- **Understandability:** implementation and documentation should be suitable for a beginner learning professional engineering practices.
- **Security:** secrets are environment-provided; uploads, downloads, and service calls are authenticated and validated.
- **Reliability:** jobs have durable status and failure metadata, and transient failures are observable.
- **Performance:** the application should avoid blocking web requests while a GPU generation runs.
- **Scalability:** the API should be able to support asynchronous job processing and replaceable storage/service mechanisms.
- **Observability:** logs should contain correlation identifiers without exposing prompts, secrets, or unnecessary personal data.
- **Portability:** the application should not assume that local development has the same filesystem or GPU capabilities as Colab.

## Scope and limitations

### Initial scope

- Text-to-video and image-to-video workflows using a feasible Wan 2.1 configuration.
- Optional Qwen3 prompt processing only if the selected checkpoint is compatible and useful.
- MySQL metadata persistence.
- Browser preview, download, and history.
- Protected Django-to-Colab communication.

### Limitations

- The NVIDIA T4 may not support every Wan 2.1 variant, resolution, duration, or precision mode.
- Colab sessions can be temporary and may impose connectivity, runtime, and storage constraints.
- A production-grade distributed queue, multi-GPU deployment, and high-volume scaling are outside the initial project.
- The exact output storage/transfer mechanism remains an implementation decision after feasibility testing.
- Authentication can initially be minimal, but authorization boundaries must be designed before exposing private outputs.
- Whisper speech-to-text is outside the initial scope and may be considered later.

## Expected outcomes

- A documented and testable web application foundation.
- Evidence-based model feasibility findings for the available T4 and checkpoint.
- A working end-to-end flow once later implementation phases are completed.
- Clear job lifecycle, error handling, security, and test documentation.
- A reproducible demonstration that does not depend on unstated filesystem or model assumptions.

