# AI service integration boundary

The AI inference service will run separately in Google Colab with access to the selected Wan 2.1 checkpoint and, optionally, a compatible Qwen3 checkpoint. This directory is for the Django-side client, request/response schemas, and integration documentation—not for model weights or an assumed Colab filesystem.

The service contract must include authentication, input transfer, job identity, progress/status reporting, failure details safe for users, and a way to transfer or reference generated output files. The exact transport and storage strategy will be selected after checkpoint feasibility testing.

The Django-side client (`client/remote.py`) already fixes these parts of the contract, which a future service must follow: job IDs of 1 to 64 ASCII letters, digits, `_` or `-` (starting with a letter or digit, not with `mock-`); JSON replies that are objects of at most 64 KiB; and a video of at most `AI_SERVICE_MAX_VIDEO_BYTES` (100 MiB by default), delivered within the `AI_SERVICE_MAX_WAIT` deadline (30 minutes by default) that covers submission, polling and download.

