# RF Alert Manager

Backend for an RF spectrum monitoring network: sensors POST observations, rules are
evaluated on event time, alerts go through OPEN → ACKNOWLEDGED → RESOLVED.
Two services talk over Redis Streams, run in Docker on an Ubuntu 24.04 VM, and are
deployed by GitHub Actions on every merge to `main`.

See [ARCHITECTURE.md](ARCHITECTURE.md), [DECISIONS.md](DECISIONS.md) and
[AI_USAGE.md](AI_USAGE.md).

## Repository layout

```
src/rfam/            application code
  engine.py          rule engine (pure Python, unit tested)
  ingest/            ingest service: observations, sensors, rules
  alerts/            alert service: consumer + alert lifecycle API
config/rules.yaml    rules loaded at startup
simulator/           traffic simulator
tests/               unit + integration tests (pytest)
Dockerfile           one image for both services (SERVICE=ingest|alerts)
compose.yaml         Caddy + ingest + alerts + PostgreSQL + Redis
deploy/              Caddyfile, deploy / rollback / smoke test scripts (run on the VM)
infra/vm/            cloud-init + provisioning script for the VM
.github/workflows/   pipeline.yml (CI/CD), rollback.yml (manual rollback)
```

## Run locally

Needs Docker with Compose v2.

```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up -d --build
curl localhost/health
```

API docs (Swagger): `http://localhost/docs` (ingest service).

## Run the tests

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest -v
```

The tests run both services against in-memory SQLite and wire them together without
Redis, so they cover the full flow (ingest → rules → alerts) with no external services.

## Run the simulator

```bash
uv run python simulator/simulate.py --url http://localhost --rate 5 --duration 150
```

Within ~2.5 minutes you should see: a power_threshold alert open for the persistent
emitter at 2437 MHz, no alert for the short burst at 2462 MHz, a multi_sensor alert at
5805 MHz, and both alerts auto-resolve after the signals stop. Point `--url` at the VM
(`http://<vm-ip>`) to run it against the deployment.

## Provision the VM

Needs [Multipass](https://multipass.run) and the GitHub CLI (`gh auth login`, admin on
the repo).

```bash
infra/vm/provision.sh                              # macOS / Linux
MULTIPASS=multipass.exe infra/vm/provision.sh      # Windows (from WSL)
```

This creates an Ubuntu 24.04 VM from `infra/vm/cloud-init.yaml` (Docker, firewall
allowing only 22 and 80, bounded logs) and registers it as a GitHub Actions
self-hosted runner with the label `rfam-vm`. Then, once:

```bash
gh secret set POSTGRES_PASSWORD          # database password used on the VM
```

### Windows (WSL2) notes

Tested on Windows with Multipass running under QEMU inside WSL2. Two things to know:

- **The VM has no internet / cloud-init fails to install Docker.** Docker sets the
  `FORWARD` firewall policy in WSL to `DROP`, which also blocks the VM's traffic.
  Fix it in WSL, then re-run the VM setup:

```bash
  sudo sysctl -w net.ipv4.ip_forward=1
  sudo iptables -P FORWARD ACCEPT
  multipass exec rfam -- sudo cloud-init clean --logs --reboot
```

  To apply it on every WSL start, add to `/etc/wsl.conf`:

```ini
  [boot]
  command="sysctl -w net.ipv4.ip_forward=1; iptables -P FORWARD ACCEPT"
```

- **Reaching the VM.** Its IP (`multipass info rfam`) is reachable from the WSL
  terminal, not necessarily from Windows apps. Run `curl`, the smoke test and the
  simulator from WSL.
  
## Deploy

Push or merge to `main`. The `pipeline` workflow then:

1. lints and runs the tests (also on every pull request),
2. builds the image and pushes `ghcr.io/<owner>/rf-alert-manager:<commit-sha>`,
3. deploys exactly that tag on the VM (`deploy/deploy.sh`),
4. runs `deploy/smoke_test.sh` against it and fails the pipeline if it is unhealthy,
5. rolls back automatically to the previous release if the smoke test fails.

To make a failing test block merges, protect `main`: Settings → Branches → add a rule
requiring the `lint & test` check.

## Roll back

- GitHub: Actions → **rollback** → Run workflow. Leave the tag empty for the previous
  release, or enter a commit SHA.
- On the VM: `~/rfam/deploy/rollback.sh [sha]`

Released tags are listed in `~/rfam/releases.log` on the VM.

## API summary

All routes go through Caddy on port 80.

| Method | Path | Service |
|---|---|---|
| POST, GET | `/api/v1/observations` | ingest |
| GET | `/api/v1/sensors` | ingest |
| GET, POST | `/api/v1/rules` | ingest |
| PUT, DELETE | `/api/v1/rules/{rule_id}` | ingest |
| GET | `/api/v1/alerts` | alerts |
| GET | `/api/v1/alerts/{alert_id}` | alerts |
| POST | `/api/v1/alerts/{alert_id}/ack` | alerts |
| POST | `/api/v1/alerts/{alert_id}/resolve` | alerts |
| GET | `/health`, `/metrics` | ingest |
| GET | `/alerts-service/health`, `/alerts-service/metrics` | alerts |

Batch POST returns `201` if all items are valid, `207` with per-index errors if some
are invalid (the valid ones are stored), and `422` if none are valid.
