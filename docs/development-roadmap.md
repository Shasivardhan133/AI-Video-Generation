# Development roadmap

1. **Project structure and requirements**  
   Establish directory boundaries, requirements, architecture, security assumptions, and proposed API/database contracts. This is the current foundation phase.
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

