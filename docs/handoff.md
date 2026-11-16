# Backend handoff

State as of 21 October 2026. The backend roadmap sections 3.1–3.9 are complete in **mock mode**. This page says where to start, what works, what does not, and which decisions are still open. Details live in the linked documents.

## Where to start

| Need | Document |
|---|---|
| Set up, configure, run, test, troubleshoot | [backend/README.md](../backend/README.md) |
| Integrate a client with the API | [api-documentation.md](api-documentation.md) |
| How the parts fit together | [architecture.md](architecture.md) |
| Tables, indexes, statuses, retention | [database-design.md](database-design.md) |
| Secrets, CSRF, ownership, media, deployment risks | [security.md](security.md) |
| What is tested, and what is not | [testing-plan.md](testing-plan.md) |
| Phase status | [development-roadmap.md](development-roadmap.md) |
| Proposed Wan 2.1 / Colab integration (not implemented) | [ai-integration-plan.md](ai-integration-plan.md) |
| How the page talks to the API | [frontend/README.md](../frontend/README.md) |

## Generation: three different things

| | Status | Evidence |
|---|---|---|
| **Mock generation** | **Works.** `MockClient` renders an FFmpeg test pattern at the requested size, length and frame rate. The API flags it with `result.is_mock: true`, and the page labels it "Mock test video · not AI-generated". | Backend tests render real FFmpeg videos. The development database holds 25 jobs (19 text, 6 image) completed this way, each with its video file (checked 21 October 2026). |
| **Remote AI client** | **Implemented, tested only against a fake service.** `RemoteClient` (`ai_service/client/remote.py`) covers authentication, submit, poll, download, the overall deadline, the video size cap, MP4 checks and error-code sanitising. | `backend/tests/test_remote_client.py`, against a fake HTTP service on `127.0.0.1`. |
| **Real AI generation** (Wan 2.1 on Google Colab) | **Not implemented.** There is no Colab service: `ai_service/colab/` exists but is empty. No model profile has been run on the T4 (`t4_verified` is `false` for both). | — |

The application must not be described as generating AI videos until the third row is done and verified.

## Everyday commands

Run these from `backend/`, with the virtual environment active.

| Task | Command |
|---|---|
| Web app, API and one worker thread | `python manage.py runserver`, then open <http://127.0.0.1:8000/> |
| A separate worker | `python manage.py run_generation_worker [--poll-seconds N]` |
| Recovery cycle without a worker | `python manage.py expire_stale_jobs [--dry-run]` |
| Orphaned-media report (read-only) | `python manage.py find_orphaned_media [--json] [--grace-minutes N]` |
| Test suite (test database and temporary media only) | `python manage.py test tests` |
| Configuration and model checks | `python manage.py check` and `python manage.py makemigrations --check --dry-run` |

## Security and operations in short

- **Secrets.**
  - Secrets come from environment variables or the git-ignored `.env`.
  - `.env.example` holds placeholders only.
  - With `DJANGO_DEBUG` off, Django refuses to start without a real `DJANGO_SECRET_KEY`.
- **CSRF and sessions.** Every `POST` needs the token from `GET /api/v1/csrf/`. Guests own jobs through their session cookie, and signed-in users through their account. Anyone else gets `404`, the same as for a missing job.
- **Media.**
  - Media is never served by URL. Videos are streamed only after the ownership check.
  - A stored reference is used only if it resolves to a regular file inside `MEDIA_ROOT`.
  - Keep `MEDIA_ROOT` writable only by the application account.
- **Retention (demo policy).**
  - Uploaded images and generated videos are kept indefinitely; nothing deletes media automatically.
  - `find_orphaned_media` only reports: the decision to remove a file needs a person who has checked that nothing still uses it, so the tool never deletes.
  - Revisit the policy before public deployment.
- **Tests.**
  - The suite runs on `test_ai_video_generator` and temporary media folders.
  - It forces mock mode with no AI-service URL or key, whatever `.env` says.
  - It refuses to run if storage points at the real `media/` folder.
- **Before public deployment.** See [backend/README.md, Production deployment](../backend/README.md#production-deployment): HTTPS settings (4 `check --deploy` warnings today), proxy headers, a body-size limit at the proxy, a shared cache for rate limits, a real web server, and supervised workers.

## Known limitations

Blockers are marked. Everything else is acceptable for the local final-year demo, but should be reviewed before any public use.

| Limitation | Severity | Next step |
|---|---|---|
| No real AI service; only mock videos | **Blocker for a real-AI demo**, not for backend acceptance | Section 4 (Wan 2.1 / Colab), after T4 feasibility |
| The current frontend has not been checked in a browser since its last changes, and the repository has no frontend automated tests | Medium | Run the [manual browser check](#manual-browser-check) before the demo |
| History shows only the newest 30 jobs (`main.js` asks for `page_size=30` and never pages) | Low | Owner decision: add "load more", or keep |
| Prompt length: **fixed in code on 21 October 2026, not yet browser-verified.** The page now refuses a prompt over 1000 characters before sending it, with the message "Prompts can be at most 1000 characters. Yours has N; please shorten it." It counts like the server (an emoji is one character); `node --check` and a counting comparison with Python passed. | Low | Include in the browser check |
| Deployment hardening (HTTPS settings, proxy, shared cache, web server) not done | **Blocker for public deployment** only | Settings change when deploying |
| `MEDIA_ROOT` is not validated at startup; an empty value makes it the project folder. The media audit refuses such a root | Low | Owner decision: validate in `settings.py` |
| Image pixels are not decoded; only headers are checked | Low | Optional: re-encode with Pillow (new dependency) |
| Media code assumes local filesystem storage | Low | Revisit with production storage |
| Path check and file open are not atomic. Only someone who can already write to `MEDIA_ROOT` could exploit this | Low | Keep `MEDIA_ROOT` private |
| The remote client also accepts `http://` service URLs | Low | Owner decision: require `https://` |
| Worker crash logs can include a full input path, which contains the prompt keyword | Low | Owner decision on log policy |
| No automatic job retry; running jobs cannot be cancelled | Low | Needs an idempotent AI-service contract |
| A crashed worker's job blocks its owner for about 5 minutes. Editing a Python file while `runserver` is running restarts the in-process worker, so a running job ends as `worker_interrupted` | Low | Expected lease behaviour; avoid edits during demos |
| Guests lose their jobs when their session cookie is cleared or expires | Low | Accounts are a deferred decision |
| History order among jobs with an identical `created_at` has no explicit tie-breaker (it was not reproducible on MySQL 8.0) | Low | Optional: order by `-created_at, -id` in the list view |
| MySQL named locks are server-wide, so a test user could share a lock name with a real user with the same ID (the development database has no users) | Low | Optional: include the database name in the lock name |
| Some timing-sensitive tests could fail on a heavily loaded machine; coverage percentage not measured | Low | Optional: install `coverage` if approved |

## Decisions awaiting the project owner

1. History paging in the frontend (see above), and the Wan 2.1 integration decisions D1–D6 in [ai-integration-plan.md](ai-integration-plan.md#8-decisions-needed).
2. Whether to validate `MEDIA_ROOT` at startup, and require `https://` for the AI service.
3. Log policy for worker crash details.
4. Whether to install a coverage tool.
5. HTTPS and deployment settings, when a public deployment is planned.

## Manual browser check

Run against <http://127.0.0.1:8000/> in mock mode:

- [ ] **Text prompt.** The job appears, completes, and the video plays with the "Mock test video" label.
- [ ] **Long prompt.** Paste more than 1000 characters and press Send. The message names the limit, no chat turn or job is created, and the error clears once the prompt is edited.
- [ ] **Seeking.** The browser's network panel shows `206` responses for `/video/`.
- [ ] **Download.** It works, and the file is named `geetha-mock-<id>.mp4`.
- [ ] **Image prompt.** A PNG, JPEG or WebP image, with and without a prompt, completes.
- [ ] **Cancel (if possible).** A job normally stays queued for only about 2 seconds before the worker claims it, so the Cancel button may disappear before you can click it. Don't stop the worker or change the running server to force a queued state. If you manage to click Cancel, the chat and the preview panel should both say "Video cancelled". If not, record the step as "not exercised in the browser". Cancellation itself is covered by the backend tests (`tests/test_cancel_jobs.py`, including the CSRF-enforced path).
- [ ] **Reload.** After reloading the page, history and any active job come back.
- [ ] **Private window.** None of the first window's jobs are visible, and pasting one of its `/video/` URLs returns `404`.
