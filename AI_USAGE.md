# AI usage

## Tools

- **Claude (claude.ai)**: planning the architecture, documentation drafts, and step-by-step help while debugging my VM.

## What I used it for

- Breaking the task into parts and choosing the stack: FastAPI, PostgreSQL,
  Redis Streams, Caddy, Multipass + cloud-init, GitHub Actions with a self-hosted runner.
- Drafting README, ARCHITECTURE and DECISIONS.
- Debugging the problems below. The AI suggested diagnostic commands, I ran them on my
  machine and fed back the output, and we narrowed the cause down together.

## How I checked the output

- Ran the linter and the test suite (23 unit and integration tests) locally and in CI.
- Ran the full stack locally with Docker Compose and the smoke test; checked the
  Dockerfile requirements by hand (non-root user, image size, health check) and that
  database data survives `docker compose down` / `up`.
- Created the VM from `infra/vm/cloud-init.yaml`, ran the stack on it, ran the smoke test
  against the VM's IP, rebooted the VM to confirm everything starts on its own, and
  checked the firewall rules and log limits.
- Reviewed each file before committing it, and committed in stages.

## Where it was wrong, and what I changed

### 1. Smoke test timed out on the very first start

**Symptom.** The first `deploy/smoke_test.sh` run failed with
`/health not healthy after 60s`, but `curl localhost/health` returned 200 a minute later.

**Cause.** Not a code bug. On the first start PostgreSQL initialises its data directory,
the services create their tables, and Caddy only starts after both app services pass
their health checks. Together this took longer than the script's 60-second wait.

**Fix.** Increased the wait to 120 seconds. In the pipeline this can't cause a false
failure anyway: `deploy.sh` runs `docker compose up --wait`, which waits for all health
checks before the smoke test starts.

### 2. Multipass could not load the cloud-init file (Windows + WSL)

**Symptom.** `multipass launch ... --cloud-init <file>` failed with "bad file" /
"could not load cloud-init configuration".

**Cause.** On my machine Multipass runs with the QEMU driver inside WSL2. The AI first
assumed the Windows version of Multipass and gave me a Windows path
(`C:\Users\Public\...`), which the Multipass inside WSL cannot read. Its second
suggestion, piping the file in with `--cloud-init -`, did create the VM, but the VM
received an **empty** configuration (`#cloud-config {}`). I found this by running
`cloud-init query userdata` in the VM. Docker, the firewall and the log limits were
never installed.

**Fix.** Passed the file with a full path under my home folder. Multipass is installed as
a snap, which may only read files under `$HOME`. I also changed `infra/vm/provision.sh`
to use an absolute path and to stop with a clear error if the VM receives an empty
configuration.

**Lesson.** Verify that the configuration actually reached the VM
(`cloud-init query userdata`, `cloud-init status --long`) instead of trusting that the
launch command succeeded.

### 3. The VM had no internet, so cloud-init could not install Docker

**Symptom.** `cloud-init status` reported `error`, and `docker` was "command not found" in
the VM. Inside the VM, `ping 8.8.8.8` printed only the header line and never got a reply.

**Cause.** The VM's traffic is routed through WSL. In WSL the firewall's `FORWARD` chain
had the policy `DROP` (Docker sets this when it starts), and IP forwarding was off, so
every packet from the VM to the internet was dropped and `apt` could not download
packages.

**Fix.** In WSL:

    sudo sysctl -w net.ipv4.ip_forward=1
    sudo iptables -P FORWARD ACCEPT

Then I re-ran the whole VM setup without recreating the VM:

    multipass exec rfam -- sudo cloud-init clean --logs --reboot

To keep it working after WSL restarts, I added the two settings to `/etc/wsl.conf`:

    [boot]
    command="sysctl -w net.ipv4.ip_forward=1; iptables -P FORWARD ACCEPT"

I also made `provision.sh` check that Docker got installed and, if not, print these exact
commands.

**Trade-off.** `FORWARD ACCEPT` is fine on a local development machine. On a shared or
production host I would add a narrow rule that only allows the Multipass bridge network.

## What I made sure I understand

- The rule engine: why a short burst never opens an alert (pending vs. confirmed
  conditions, `min_duration_s`, `max_gap_s`).
- The event-time watermark: why auto-resolve uses observation time, not the clock, and
  how late observations attach to an already-resolved alert.
- Deduplication: alerts are keyed by rule + frequency bucket, and observations are linked
  by id, so redelivered messages are never counted twice.
- The multi-stage Dockerfile, and why only Caddy publishes a port.
- Why the GitHub runner lives on the VM (it connects out to GitHub, so a local VM that
  isn't reachable from the internet can still be deployed to).
- How rollback picks the previous version (`releases.log` on the VM).
