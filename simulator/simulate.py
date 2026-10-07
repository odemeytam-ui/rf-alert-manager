"""RF traffic simulator.

Sends real-time observations to the service and prints how alerts evolve.

    uv run python simulator/simulate.py --url http://localhost --rate 5 --duration 150

Timeline (seconds from start; all scenarios run together):
  always     background: 6 sensors, weak / out-of-band signals -> no alerts
  5  - 45    strong persistent emitter at 2437 MHz (sensor-03)  -> power_threshold alert,
             then it disappears -> auto-resolves ~30 s later (resolve_after_s)
  20 - 23    short burst at 2462 MHz (sensor-05)                 -> no alert
  60 - 80    signal at 5805 MHz seen by 3 sensors                -> multi_sensor alert,
             then gone -> auto-resolves
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import UTC, datetime

import httpx

SENSORS = {
    "sensor-01": (32.08, 34.78),
    "sensor-02": (32.07, 34.79),
    "sensor-03": (32.09, 34.77),
    "sensor-04": (32.06, 34.80),
    "sensor-05": (32.10, 34.76),
    "sensor-06": (32.05, 34.81),
}


def obs(sensor: str, freq: float, power: float, bw: float = 200) -> dict:
    lat, lon = SENSORS[sensor]
    return {
        "sensor_id": sensor,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "frequency_mhz": round(freq, 4),
        "bandwidth_khz": bw,
        "power_dbm": round(power, 1),
        "location": {"lat": lat, "lon": lon},
    }


def background(n: int) -> list[dict]:
    out = []
    for _ in range(n):
        sensor = random.choice(list(SENSORS))
        if random.random() < 0.5:  # weak signal inside the ISM band, below threshold
            out.append(obs(sensor, random.uniform(2400, 2483), random.uniform(-95, -65), 20000))
        else:  # ordinary traffic outside any protected band
            out.append(obs(sensor, random.uniform(400, 950), random.uniform(-90, -40)))
    return out


def scenario(t: float) -> tuple[list[dict], list[str]]:
    batch, notes = [], []
    if 5 <= t < 45:
        freq, power = 2437 + random.uniform(-0.01, 0.01), -40 + random.uniform(-3, 3)
        batch.append(obs("sensor-03", freq, power, 20000))
        notes.append("persistent emitter ON @2437")
    if 20 <= t < 23:
        batch.append(obs("sensor-05", 2462, -35, 20000))
        notes.append("short burst @2462")
    if 60 <= t < 80:
        for s in ("sensor-01", "sensor-02", "sensor-04"):
            batch.append(obs(s, 5805 + random.uniform(-0.04, 0.04), -60 + random.uniform(-3, 3)))
        notes.append("multi-sensor signal @5805")
    return batch, notes


def print_alerts(client: httpx.Client, url: str) -> None:
    try:
        alerts = client.get(f"{url}/api/v1/alerts").json()
    except httpx.HTTPError as e:
        print(f"   (could not read alerts: {e})")
        return
    if not alerts:
        print("   alerts: none")
    for a in alerts:
        print(
            f"   alert {a['alert_id']} {a['rule_id']:<22} {a['state']:<12} "
            f"{a['frequency_mhz']:>8.2f} MHz  sensors={','.join(a['sensor_ids'])} "
            f"occ={a['occurrences']} peak={a['peak_power_dbm']} dBm"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default="http://localhost", help="base URL of the service")
    p.add_argument("--rate", type=float, default=5, help="background observations per second")
    p.add_argument("--duration", type=float, default=150, help="seconds to run")
    p.add_argument("--report-every", type=float, default=5, help="print alerts every N seconds")
    args = p.parse_args()

    url = args.url.rstrip("/")
    start = time.monotonic()
    next_report = 0.0
    with httpx.Client(timeout=5) as client:
        while (t := time.monotonic() - start) < args.duration:
            extra, notes = scenario(t)
            n_bg = max(0, int(args.rate)) + (random.random() < args.rate % 1)
            batch = background(n_bg) + extra
            try:
                r = client.post(f"{url}/api/v1/observations", json=batch)
                status = r.status_code
            except httpx.HTTPError as e:
                status = f"ERR {e}"
            if t >= next_report:
                print(f"[t={t:5.1f}s] sent {len(batch)} obs -> {status}  {' | '.join(notes)}")
                print_alerts(client, url)
                next_report += args.report_every
            time.sleep(max(0.0, 1 - ((time.monotonic() - start) - t)))
        print("done. final alerts:")
        print_alerts(client, url)


if __name__ == "__main__":
    main()
