# Wan 2.1 on Google Colab: integration plan

> **Status: PROPOSAL, awaiting approval. Nothing on this page is implemented.**
> - Real AI generation does not exist yet: `ai_service/colab/` is empty, and no profile has `t4_verified = True`.
> - The Django side is already in place: `RemoteClient` (`ai_service/client/remote.py`), the worker and the job lifecycle.
> - What is missing is the service in Colab, and evidence that Wan 2.1 runs acceptably on a T4.

## 1. What exists and what is missing

| Part | State |
|---|---|
| **Mock generation** | Works. FFmpeg test pattern, flagged `is_mock`; **not AI output**. |
| **Remote client** (Django side) | Implemented and tested **only against a fake HTTP service** on `127.0.0.1`. It covers:<br>• bearer authentication;<br>• submit, poll and download;<br>• the overall deadline;<br>• the 100 MiB video cap;<br>• MP4 checks;<br>• job-ID validation;<br>• error-code sanitising. |
| Worker and job lifecycle | Implemented and tested. It already sends everything a service needs (below). No change is expected. |
| **Colab service** (`ai_service/colab/`) | **Missing.** Nothing implements the server side of the contract. |
| **Wan 2.1 on a T4** | **Not measured.** Unknown: memory, time, precision support, frame limits. |
| Image to video on a T4 | **Likely infeasible as profiled.** The only Wan 2.1 image-to-video model is 14B (see section 6). |
| Configuration | Ready: `AI_SERVICE_MODE=remote`, `AI_SERVICE_BASE_URL`, `AI_SERVICE_API_KEY` and the timeout settings exist in `.env.example`. |

## 2. Architecture

```text
Browser ── Django API ── MySQL (jobs)
                 │
           generation worker ── RemoteClient ──HTTPS + Bearer──▶ tunnel ──▶ Colab service ──▶ Wan 2.1 (GPU)
                 │                                                            │
             media/outputs ◀──────────── downloads the verified MP4 ◀─────────┘
```

- **Only the worker talks to Colab.** The browser never learns the service URL or key, and the API is unchanged.
- **Colab has no inbound ports.** The service is reached through a tunnel that provides HTTPS: a Cloudflare quick tunnel, which needs no account but gets a new URL every session, or ngrok, which needs an account token. The choice needs your approval.
- **The service runs one generation at a time** (one GPU) and queues the rest in order.

## 3. Service contract

The contract is already fixed by the client's docstring in `ai_service/client/remote.py`. The service must implement exactly this:

| Request | Response |
|---|---|
| `POST /v1/jobs`, multipart: `request` (JSON text) and optional `image` (the uploaded PNG/JPEG/WebP) | `202 {"job_id": "<id>"}` |
| `GET /v1/jobs/{id}` | `200 {"status": "queued"\|"processing"\|"completed"\|"failed", "error": null \| {"code", "message"}}` |
| `GET /v1/jobs/{id}/video` | `200 video/mp4`, with `Content-Length` |
| `GET /v1/health` | `200 {"status": "ok", "gpu": "<name>"}` |

**Every request** carries `Authorization: Bearer <key>`. A missing or wrong key gets `401`, which the client maps to `service_auth_failed`. Compare keys with `hmac.compare_digest`.

**The `request` JSON** sent by the worker (`GenerationRequest.payload()`) is:

```json
{"job_id": "<Django job UUID>", "mode": "text_to_video", "prompt": "…, cinematic lighting, film look",
 "wan_task": "t2v-1.3B", "width": 832, "height": 480, "frame_count": 81, "fps": 16,
 "seed": null, "enhance_prompt": false, "sample_options": {"sample_shift": 8, "sample_guide_scale": 6}}
```

The service must:

- **Validate everything again against its own allow-list:** `wan_task`, `width` × `height`, `frame_count`, `fps`. Reject anything else with `400`, which the client maps to `service_rejected`.
- **Never pass the prompt through a shell.** Call Wan 2.1's Python API, or `generate.py` with an argument list, never a shell string.
- **Use job IDs that the client accepts.** IDs must be 1–64 characters from `[A-Za-z0-9_-]`, start with a letter or digit, and not start with `mock-`. A UUID4 qualifies.
- **Keep failure messages internal.** Use failure codes of 1–50 characters from `[a-z0-9_]`, for example `out_of_memory`, `generation_failed`, `model_not_loaded`. The service's own messages are logged by Django, never shown to users.
- **Return a playable MP4 of at most 100 MiB.** The file needs the `ftyp` box at offset 4 and at least 1 KB; write it with `+faststart`.
- **Honour `enhance_prompt` correctly.** Qwen3 is not planned for the first version, so the service must fail such a job with a clear code such as `prompt_enhancement_unavailable`, not ignore the flag. Today the API accepts `enhance_prompt: true` for the text profile, but the frontend never sends it (decision D4).

Downloading is **pull-based**: Django downloads the video, and nothing relies on a shared filesystem. The worker saves the file under `media/outputs/` with the existing safeguards: a new name, partial files removed, and the result recorded only while the job is still `processing`.

## 4. Failure handling, already implemented on the Django side

| Situation | What happens |
|---|---|
| Service unreachable, tunnel down, Colab runtime ended | Submission failure gives `service_unavailable`. During polling, 3 failed checks in a row fail the job. |
| Slow generation | The whole submit, poll and download must finish within `AI_SERVICE_MAX_WAIT` (default 1800 s), otherwise `service_timeout`. The worker's heartbeat keeps the job alive meanwhile. |
| Oversized or invalid video | `output_too_large` or `invalid_output`. Partial files are deleted. |
| Worker crash or restart mid-job | Lease recovery fails the job (`worker_interrupted`) after about 5 minutes. **The service keeps generating**: there is no remote cancel endpoint, so the GPU time is wasted. |
| User cancels | Only while `queued`, before anything is sent to Colab. A processing job cannot be cancelled. |

**Gaps this plan accepts for version 1:**

- no remote cancel endpoint;
- no automatic retry;
- the tunnel URL changes every Colab session, so `.env` must be updated and the Django server restarted;
- service outputs should be deleted after a time limit, because Colab disk is temporary anyway.

## 5. Configuration and secrets

- **Key.** Generate one per demo session, for example with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
  - Colab side: store it in **Colab Secrets** (`google.colab.userdata`), never in notebook cells, outputs or Git.
  - Django side: put it in `.env` as `AI_SERVICE_API_KEY`.
- **Settings.** Set `AI_SERVICE_MODE=remote` and `AI_SERVICE_BASE_URL=<the tunnel's https URL>`, then restart the server (settings are read at startup).
- **HTTPS only.** Requiring `https://` in `RemoteClient` is decision D3.
- **Development only.** A tunnel URL is public. Treat it as development-only, rely on the key and not on the URL being obscure, and stop the tunnel after the demo ([security.md](security.md#temporary-tunnel-risks)).

## 6. GPU, time and storage constraints, to be measured before any claim

| Item | What is known | What must be measured on the T4 |
|---|---|---|
| T2V-1.3B memory | The Wan 2.1 release states about 8.2 GB of VRAM at 480p, with model offloading and T5 on the CPU. Not measured here. | Peak VRAM and system RAM. Free Colab gives about 12–13 GB of RAM, and T5 on the CPU needs a large share of it. |
| Precision | The T4 (Turing) has no native bfloat16, which Wan 2.1 uses by default. FlashAttention does not support Turing. | Whether it runs at all; whether float16 or a slower fallback is needed; output quality. |
| Time | The release quotes about 4 minutes for a 5-second 480p clip on an RTX 4090. A T4 is many times slower. | Seconds per clip for 33, 49 and 81 frames. If 81 frames exceeds about 25 minutes, lower the profile's maximum frame count, or raise `AI_SERVICE_MAX_WAIT` and `GENERATION_QUEUE_TIMEOUT_SECONDS` together. |
| I2V-14B | 14B parameters do not fit a 16 GB T4 in bfloat16 without heavy offloading or quantization. | Probably **not feasible on a free T4**. It needs a larger GPU (Colab Pro L4/A100) or a different approach. Approval needed. |
| Checkpoints | Already in your Google Drive, per the project brief. Loading them from Drive takes time. | Load time per session; local disk space. |
| Colab limits | Sessions end after idle time or a maximum runtime, and T4 availability on the free tier is not guaranteed. | Plan demos around one session; keep the notebook re-runnable. |

## 7. Minimum implementation sequence (after approval)

1. **Feasibility (phase 2, manual, in Colab, no Django).** Load T2V-1.3B from Drive and generate one short clip (17–33 frames). Record VRAM, RAM, time and precision workarounds.
2. **Write the service** in `ai_service/colab/`:
   - a small HTTP server implementing section 3;
   - a GPU-free "stub generator" mode.
   - Prefer the standard library (`http.server`), so neither Colab nor Django needs new packages; otherwise approve FastAPI for the Colab side only.
3. **Contract test locally, without a GPU.** Run the service in stub mode on `127.0.0.1` and point the existing `RemoteClient` tests at it. This proves both sides agree before any GPU time is spent.
4. **Update profiles from measurements** (`backend/apps/generator/profiles.py`): frame limits, `sample_options`, and `t4_verified = True` only for what actually ran. Decide image to video separately.
5. **First real run** with your approval:
   - start the Colab session and the tunnel;
   - set `.env`;
   - restart Django yourself;
   - submit one text job and check the video, its timing and the logs.
6. **Record the evidence** in [testing-plan.md](testing-plan.md) and the roadmap. Only then describe the application as generating AI video.

## 8. Decisions needed

| ID | Decision |
|---|---|
| D1 | Approve this design and the first feasibility run in Colab (your GPU time and account). |
| D2 | Tunnel provider: Cloudflare quick tunnel, or ngrok with an account token. |
| D3 | Require `https://` for `AI_SERVICE_BASE_URL` in `RemoteClient`. |
| D4 | Prompt enhancement: mark the text profile `supports_prompt_enhancement=False` until Qwen3 exists, or leave it (the service would reject such jobs). |
| D5 | Image to video: accept "not on the free T4", use a paid GPU, or drop it from the demo. |
| D6 | The standard-library server, or FastAPI on the Colab side. |
