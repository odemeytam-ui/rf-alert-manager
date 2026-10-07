# Decisions

## Stack

**Python + FastAPI.** Pydantic models give the required validation (ranges, required
fields, UTC timestamps) declaratively, with clear error messages, and FastAPI produces
OpenAPI docs for free. Sync endpoints are enough at this scale and keep the code simple.

**PostgreSQL.** Observations are queried by sensor, frequency range and time range:
a relational table with composite indexes fits directly. It is the boring, well-known
choice and runs comfortably in a small container. SQLAlchemy keeps the code portable,
which lets the tests use in-memory SQLite.

**Redis Streams as the broker.** Requirements: asynchronous, survives restarts, the
consumer can crash without losing messages, low operational cost on one small VM.
Redis Streams with a consumer group gives at-least-once delivery (messages stay pending
until `XACK`, and are re-read after a crash), persistence via AOF, and a bounded stream
length. It is one small container with no tuning.
*Alternatives:* RabbitMQ (richer routing and dead-lettering, but more to configure,
and none of that is needed here); Kafka (partitioned replayable log, far too heavy for
one VM); NATS JetStream (a good fit too, less widely known).

**Caddy as reverse proxy.** The whole config is a few lines and it can add HTTPS later
with one line once there is a domain.

**One image for both services.** Same dependencies and code; `SERVICE` selects the app.
One build and one tag per commit keeps "deploy exactly this commit" trivial.

## Service split and data ownership

- **ingest**: observations, sensors, rules, rule evaluation.
- **alerts**: alerts and their lifecycle.

Rule evaluation sits in the ingest service because it needs the rule definitions and
the stream of observations; the alert service only needs the *result*. The alert service
stores its own copy of contributing observations, so it never reads the ingest tables.
Both use one PostgreSQL instance (cheaper on one VM) but separate tables; moving to two
databases is a config change.

## Batch validation: partial accept

In a batch, valid items are stored and invalid ones are reported by index (HTTP 207).
A single bad reading from a sensor should not drop the good readings sent with it.
A single object that is invalid → 422. A batch with no valid item → 422. Batches are
capped at 1000 items (413).

## Event time, late and out-of-order observations

- All logic uses the observation `timestamp`, never arrival time.
- **Watermark**: the ingest service tracks the highest event time seen and publishes it
  after every request. The alert service uses it as its clock: an alert auto-resolves
  when `last_seen + resolve_after_s < watermark`. Replaying old data therefore behaves
  exactly like live data.
- **Out of order within a condition**: the pending power_threshold condition keeps the
  min and max event time it has seen, so shuffled observations still add up correctly.
- **Late after resolution**: when an alert is resolved, the watermark at that moment is
  stored (`resolved_event_time`). An observation older than that which arrives later
  is attached to the resolved alert (it really happened during it) instead of
  reopening it or creating a new one. Newer observations open a new alert, as required.
- **Limitation**: if *all* sensors go silent, the watermark stops and open alerts do not
  auto-resolve until traffic resumes. With many always-on sensors this is acceptable;
  a wall-clock fallback (advance the watermark by elapsed time when idle) is the fix.
  A sensor with a clock far in the future would also push the watermark too far;
  rejecting timestamps more than N seconds ahead of now would guard against that.

## Rule semantics

- **min_duration_s**: the condition must span that much event time. `max_gap_s`
  (default 5 s, added to the model) defines "persistent": a longer gap while pending
  resets it. Without it, two short bursts a minute apart would add up to an alert.
- Once confirmed, the condition stays alive as long as the alert would
  (`resolve_after_s`), so an emitter that flickers keeps updating the same alert.
- **Same signal** = same rule + same frequency bucket of width
  `frequency_tolerance_khz` (default 100 kHz). Simple and deterministic; a signal sitting
  exactly on a bucket boundary could split into two alerts.
- Rules are loaded from `config/rules.yaml` at startup **only if they do not exist** in
  the database, so changes made through the API survive restarts.
- `PUT /rules/{id}` merges the body into the existing rule, then validates the result,
  so `{"enabled": false}` works. Strict PUT semantics would require the full body.

## VM: Multipass + cloud-init, Ubuntu 24.04, running locally

- Free, no cloud account, and recreated with one command (`infra/vm/provision.sh`).
- cloud-init is the same format cloud providers accept, so `infra/vm/cloud-init.yaml`
  can provision an EC2 / Oracle / Hetzner VM unchanged.
- The VM is defined entirely as code: packages, firewall (only 22 and 80), bounded
  Docker and journal logs, and the GitHub runner installer all come from cloud-init.
- `provision.sh` verifies the result instead of assuming it: it stops with a clear error
  if the VM received an empty cloud-init configuration, or if Docker was not installed.

### My environment: Windows + WSL2

- Multipass runs with the QEMU driver inside WSL2. The VM's IP is reachable from WSL,
  not necessarily from Windows apps, so `curl`, the smoke test and the simulator run
  from WSL.
- Docker sets the `FORWARD` firewall policy in WSL to `DROP`, which also blocked the VM's
  internet access, so cloud-init could not install packages. Fixed by enabling IP
  forwarding and setting the policy to `ACCEPT`, applied at every WSL start via
  `/etc/wsl.conf`. Fine for a local machine; on a shared host I would allow only the
  Multipass bridge network. (Details in AI_USAGE.md.)
- None of this affects CI/CD: the self-hosted runner inside the VM only needs
  **outbound** internet access to reach GitHub and GHCR.
  
## CI/CD: GitHub Actions + self-hosted runner on the VM

The VM is not reachable from the internet. Options considered:

| Option | Why not / why |
|---|---|
| Expose the VM (port forwarding, public IP) and SSH from GitHub | Opens SSH to the internet, needs SSH keys in secrets, home router config |
| Tunnel (Tailscale, Cloudflare Tunnel, ngrok) | Works, but one more service and account to manage |
| **Self-hosted runner on the VM** (chosen) | The runner makes outbound HTTPS connections to GitHub and long-polls for jobs. No inbound ports, no SSH keys, and the deploy job runs on the target itself. |

Tests and image builds still run on GitHub-hosted runners; only the deploy job runs on
the VM (`runs-on: [self-hosted, rfam-vm]`).

**Risk:** a self-hosted runner executes workflow code from the repository. On a public
repo, a pull request from a fork could try to run on it. Mitigations: the deploy job
only runs on pushes to `main`, and GitHub by default requires approval for workflows
from outside contributors. For a real system, use a private repo or ephemeral runners.

**Deploy / verify / rollback**
- The image is tagged with the commit SHA; the deploy job deploys exactly that tag.
- `docker compose up --wait` waits for the container health checks, then
  `smoke_test.sh` checks both services, writes a test observation and reads the API.
- Every successful deploy is appended to `~/rfam/releases.log`. Rollback redeploys the
  previous tag (or a given one). It runs automatically when the smoke test fails, and
  manually via the `rollback` workflow or the script on the VM.
- Database schema is created with `create_all` (additive only), so rolling back the
  image does not need a schema rollback in this version.

## Secrets

- No secrets in the repo or the image. `.env` is git-ignored; `.env.example` documents
  the variables.
- `POSTGRES_PASSWORD` is a GitHub Actions secret, written to `~/rfam/.env` (mode 600)
  on the VM during deploy.
- GHCR authentication uses the workflow's short-lived `GITHUB_TOKEN`.

## Not production-ready

- **Single instance of each service.** The rule engine's pending state is in memory:
  a restart loses pending (not yet confirmed) conditions, and two ingest replicas
  would each see only part of the traffic. Fix: shard by signal, or keep the state
  in Redis.
- **Ingest serializes writes per instance** (a lock) to keep sensor upserts and event
  order consistent. Fine for a demo; throughput is limited.
- **Publish after commit, without an outbox.** If Redis is down, observations are
  stored but their evaluation is lost (counted in `rfam_publish_errors_total`).
  Fix: transactional outbox table + relay.
- **No schema migrations** (`create_all`). Use Alembic.
- **No authentication** on the API, no TLS (HTTP only on a local VM).
- **Poison messages** are logged and acknowledged; a dead-letter stream would be better.
- **Observation retention**: the table grows forever. Partition by time (or use
  TimescaleDB) and drop old partitions.
- Smoke test writes one observation (sensor `smoke-test`, out of every band) to the
  production data.

## With more time

Outbox pattern, Alembic, API keys per sensor, wall-clock watermark fallback,
Prometheus + Grafana dashboards as code, webhook notifications with retries,
sensor_silence rule, load test with k6 or Locust, zero-downtime deploys (two app
replicas behind Caddy, rolling update).
