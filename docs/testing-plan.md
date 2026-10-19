# Testing plan

## Frontend testing

- Verify prompt, image, and settings validation before submission.
- Verify image preview handles valid and invalid files.
- Verify loading, accepted, processing, completed, and failed states.
- Verify history rendering, empty history, pagination, playback, and download controls.
- Verify API errors are shown without exposing raw server diagnostics.

## Backend API testing

- Test each proposed request shape once endpoints exist.
- Test authentication and ownership checks for jobs and media.
- Test validation of modes, prompts, image types, image dimensions, sizes, and supported settings.
- Test job state transitions and idempotency/retry behavior.
- Test safe error envelopes for malformed requests and unavailable AI service responses.
- Test that API requests do not claim completion before output verification.

## Database testing

- Test required fields, unique job identifiers, foreign keys, and status constraints.
- Test timestamp creation/update behavior and ordering used by history.
- Test user-to-job ownership queries and indexes with representative data.
- Test that media bytes are not stored in metadata columns.

## AI service testing

- Unit-test request/response schema validation without loading models.
- Test authentication, correlation identifiers, timeouts, malformed responses, and service failures.
- Run a small feasibility test for the selected Wan 2.1 configuration on the T4 before integration claims are made.
- Test output transfer/reference validation and cleanup behavior.
- Test optional Qwen3 separately so it can be disabled if incompatible or too expensive.

## End-to-end testing

- Submit a text job and follow it to a verified result.
- Submit an image job and follow it to a verified result.
- Confirm history and authorized playback/download after completion.
- Exercise failed generation, timeout, unavailable service, invalid output, and expired storage cases.
- Confirm the flow works without assuming a shared Colab/Django filesystem.

## Error handling tests

At minimum, test missing prompts, unsupported image formats, oversized files, invalid settings, unauthorized job access, duplicate/retried submissions, database failure, AI timeout, AI authentication failure, malformed AI response, missing output, and output download failure. Each case should have a stable internal category, safe user message, and useful server-side correlation data.

## Phase acceptance criteria

1. **Structure and requirements:** documented boundaries exist; no feature code or fake endpoints are present.
2. **Model feasibility:** a tested model profile, resource observations, and limitations are recorded without downloading duplicate weights.
3. **Frontend:** core forms and state/error views work against mocked/proposed contracts without claiming real generation.
4. **Django foundation:** generated Django project starts, validates requests, and exposes testable API foundations.
5. **MySQL:** migrations apply to a configured database and metadata relationships/indexes are tested.
6. **Job management:** jobs persist, transition safely, and survive normal request boundaries.
7. **Protected AI service:** authenticated request/response and failure handling are tested in Colab constraints.
8. **Text generation:** a text job reaches a real, verified output on the tested model profile.
9. **Image generation:** a validated image job reaches a real, verified output.
10. **History/reliability:** history, access controls, cleanup, retries, and output handling are tested.
11. **End-to-end:** representative success and failure scenarios pass from browser to output.
12. **Demonstration:** documentation, setup constraints, limitations, and known risks are complete.

