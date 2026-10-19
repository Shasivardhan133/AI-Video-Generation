# Security guidance

## Environment variables and secrets

Keep Django secrets, database passwords, service API keys, and tunnel credentials in environment variables or a local secret manager. `.env` files are ignored by Git; `.env.example` contains names and safe placeholders only. Never place model access tokens or credentials in frontend JavaScript, notebooks committed to the repository, logs, or documentation.

## Input validation

Validate mode, prompt length, settings, numeric ranges, and model-profile capabilities on the Django side. Treat client-side validation as a usability feature, not a security control. Normalize data before sending it to the AI service and reject unknown or unsupported parameters.

## Uploaded-file validation

Validate declared and detected content type, extension, file signature, size, dimensions, and image decodability. Use generated server-side names rather than trusting user filenames. Store uploads outside executable/static paths and consider re-encoding images with Pillow before inference. Do not pass untrusted filenames or paths to shell commands.

## File-size restrictions

Define separate limits for request body, image input, generated output, and history/download operations. Enforce limits at the web server and application layers. Reject oversized data before transferring it to Colab, and clean up temporary files after success or failure.

## API authentication between Django and AI service

Use a dedicated service credential transmitted only over an encrypted connection. Validate the service response, bind it to the expected job/correlation identifier, and rotate credentials. Do not expose the service credential to browsers. Add replay protection or short-lived signed requests if the final deployment requires it.

## Temporary tunnel risks

Colab tunnels can expose a changing public endpoint and may be reachable by unintended parties. Treat a tunnel as development-only: use strong authentication, narrow allowed operations, short-lived credentials, request limits, and no sensitive diagnostic responses. Never rely on obscurity of a tunnel URL.

## Output access controls

Check the requesting user's ownership or an explicit sharing policy before returning a video. Do not expose local filesystem paths. Prefer short-lived signed media URLs or an authenticated streaming endpoint, and remove expired outputs according to a documented retention policy.

## Safe error reporting

Return concise, actionable user messages. Keep stack traces, service tokens, raw prompts where sensitive, internal paths, and provider credentials out of responses. Correlate user-safe errors with server-side logs using a request/job identifier, while applying retention and access controls to those logs.

