# AI service integration boundary

The AI inference service will run separately in Google Colab with access to the selected Wan 2.1 checkpoint and, optionally, a compatible Qwen3 checkpoint. This directory is for the Django-side client, request/response schemas, and integration documentation—not for model weights or an assumed Colab filesystem.

The service contract must include authentication, input transfer, job identity, progress/status reporting, failure details safe for users, and a way to transfer or reference generated output files. The exact transport and storage strategy will be selected after checkpoint feasibility testing.

