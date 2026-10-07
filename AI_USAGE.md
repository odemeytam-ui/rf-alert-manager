# AI usage

> Draft. Edit this so it describes what *you* did, in your own words.

## Tools

- **Claude (claude.ai, Opus 5.5)**: planning, first version of the code, infrastructure
  files and documentation drafts.
- *(add any other tools you used, e.g. Copilot in VS Code)*

## What it was used for

- Breaking the task into steps and choosing the stack (FastAPI, PostgreSQL, Redis
  Streams, Caddy, Multipass + cloud-init, self-hosted runner).
- Generating the initial implementation of both services, the rule engine, the
  simulator, the tests, the Dockerfile, compose file, cloud-init, deploy scripts and
  workflows.
- Drafting README / ARCHITECTURE / DECISIONS.

## How the output was checked

- Ran ruff and the test suite (23 tests).
- Ran both services against real PostgreSQL and Redis behind Caddy, ran the smoke test
  and the full simulator: the persistent emitter opened one alert, the short burst
  opened none, the multi-sensor signal opened one alert, and both auto-resolved.
- Tested the deploy / rollback scripts' bookkeeping (`.env`, `releases.log`).
- *(add: building the image, provisioning the VM, the first pipeline run, rollback demo)*

## Where it was wrong or needed changes

- *(fill in as you go — examples of the kind of thing to record:)*
- *A line over the configured length limit failed `ruff check`; reformatted.*
- *(anything that failed on your machine: paths on Windows/WSL, Multipass networking,
  GHCR permissions, etc.)*
- *(design choices you questioned or changed, and why)*

## What I made sure I understand

*(list the parts you reviewed line by line — e.g. the rule engine's pending/confirmed
logic, the watermark-based auto-resolve, late observation handling, why the runner
lives on the VM, how rollback picks the previous tag.)*
