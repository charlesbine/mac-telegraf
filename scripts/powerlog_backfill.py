#!/usr/bin/env python3
"""Backfill historical 1-minute macOS Powerlog wattage data into InfluxDB v2.

Reads macOS's built-in `/var/db/powerlog/Library/BatteryLife/CurrentPowerlog.PLSQL`
and `.PLSQL.gz` archives (world-readable, no sudo required) and converts them into
InfluxDB Line Protocol matching the `mac_power` measurement emitted by
`scripts/mac_telemetry.py`.

Usage:
  python3 scripts/powerlog_backfill.py --dry-run
  INFLUXDB_URL=... INFLUXDB_TOKEN=... INFLUXDB_ORG=... INFLUXDB_BUCKET=... \
    python3 scripts/powerlog_backfill.py --host my-mac
"""

from __future__ import annotations

import argparse
import glob
import gzip
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mac_telemetry import format_line

DEFAULT_POWERLOG_DIR = Path("/var/db/powerlog/Library/BatteryLife")


def extract_powerlog_db(db_path: Path) -> list[dict[str, float | int | bool | None]]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    offset = 0.0
    try:
        row = cur.execute(
            "SELECT system FROM PLStorageOperator_EventForward_TimeOffset ORDER BY ID DESC LIMIT 1"
        ).fetchone()
        if row and row[0] is not None:
            offset = float(row[0])
    except sqlite3.OperationalError:
        pass

    try:
        adapter_rows = cur.execute(
            """
            SELECT timestamp, SystemLoad, SystemPowerIn,
                   AccumulatedSystemLoad, SystemLoadAccumulatorCount,
                   AccumulatedSystemPowerIn, SystemPowerInAccumulatorCount,
                   SystemInputVoltage, SystemInputCurrent
            FROM PLBatteryAgent_EventBackward_Adapter
            ORDER BY timestamp
            """
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return []

    battery_map: dict[int, tuple] = {}
    try:
        for brow in cur.execute(
            """
            SELECT timestamp, Level, ExternalConnected, IsCharging, Temperature, CycleCount
            FROM PLBatteryAgent_EventBackward_Battery
            """
        ).fetchall():
            battery_map[round(brow[0])] = brow
    except sqlite3.OperationalError:
        pass

    conn.close()

    records: list[dict[str, float | int | bool | None]] = []
    for r in adapter_rows:
        raw_ts = r[0]
        ts = raw_ts + offset
        b = (
            battery_map.get(round(raw_ts))
            or battery_map.get(round(raw_ts) - 1)
            or battery_map.get(round(raw_ts) + 1)
        )
        records.append(
            {
                "ts": ts,
                "sys_load_mw": r[1],
                "power_in_mw": r[2],
                "acc_load": r[3],
                "acc_load_cnt": r[4],
                "acc_pin": r[5],
                "acc_pin_cnt": r[6],
                "vin_mv": r[7],
                "iin_ma": r[8],
                "level": b[1] if b else None,
                "external_connected": bool(b[2]) if b and b[2] is not None else None,
                "is_charging": bool(b[3]) if b and b[3] is not None else None,
                "temp_raw": b[4] if b else None,
                "cycle_count": b[5] if b else None,
            }
        )
    return records


def build_lines_from_records(
    records: list[dict[str, float | int | bool | None]],
    host: str,
    host_role: str,
    model: str | None = None,
) -> list[str]:
    dedup: dict[int, dict[str, float | int | bool | None]] = {}
    for rec in records:
        ts = rec["ts"]
        if isinstance(ts, (int, float)):
            dedup[round(ts / 5)] = rec
    ordered = sorted(dedup.values(), key=lambda x: float(x["ts"] or 0.0))

    tags = {"host": host, "host_role": host_role, "os": "macos", "source": "powerlog"}
    if model:
        tags["model"] = model

    lines: list[str] = []
    for i, curr in enumerate(ordered):
        prev = ordered[i - 1] if i > 0 else None
        ts = float(curr["ts"] or 0.0)
        fields: dict[str, int | float | bool | str] = {}

        avg_load_w: float | None = None
        avg_pin_w: float | None = None
        if prev is not None:
            dt = ts - float(prev["ts"] or 0.0)
            if 10 <= dt <= 600:
                if curr["acc_load_cnt"] and prev["acc_load_cnt"]:
                    dcnt = int(curr["acc_load_cnt"]) - int(prev["acc_load_cnt"])
                    dacc = int(curr["acc_load"] or 0) - int(prev["acc_load"] or 0)
                    if dcnt > 0 and 0 <= (dacc / dcnt) <= 250000:
                        avg_load_w = round((dacc / dcnt) / 1000.0, 3)
                if curr["acc_pin_cnt"] and prev["acc_pin_cnt"]:
                    dcnt = int(curr["acc_pin_cnt"]) - int(prev["acc_pin_cnt"])
                    dacc = int(curr["acc_pin"] or 0) - int(prev["acc_pin"] or 0)
                    if dcnt > 0 and 0 <= (dacc / dcnt) <= 250000:
                        avg_pin_w = round((dacc / dcnt) / 1000.0, 3)

        if avg_load_w is None and curr["sys_load_mw"] is not None:
            avg_load_w = round(float(curr["sys_load_mw"]) / 1000.0, 3)
        if avg_pin_w is None and curr["power_in_mw"] is not None:
            avg_pin_w = round(float(curr["power_in_mw"]) / 1000.0, 3)

        if avg_load_w is not None:
            fields["system_load_w"] = avg_load_w
        if curr["sys_load_mw"] is not None:
            fields["system_load_inst_w"] = round(float(curr["sys_load_mw"]) / 1000.0, 3)
        if avg_pin_w is not None:
            fields["system_power_in_w"] = avg_pin_w
        if curr["power_in_mw"] is not None:
            fields["system_power_in_inst_w"] = round(float(curr["power_in_mw"]) / 1000.0, 3)
        if curr["vin_mv"] is not None:
            fields["input_voltage_v"] = round(float(curr["vin_mv"]) / 1000.0, 3)
        if curr["iin_ma"] is not None:
            fields["input_current_a"] = round(float(curr["iin_ma"]) / 1000.0, 3)
        if curr["level"] is not None:
            fields["battery_pct"] = round(float(curr["level"]), 2)
        if curr["external_connected"] is not None:
            fields["external_connected"] = 1 if curr["external_connected"] else 0
        if curr["is_charging"] is not None:
            fields["is_charging"] = 1 if curr["is_charging"] else 0
        if curr["temp_raw"] is not None:
            temp = float(curr["temp_raw"])
            fields["battery_temp_c"] = round(temp / 100.0 if temp > 200 else temp, 2)
        if curr["cycle_count"] is not None:
            fields["cycle_count"] = int(curr["cycle_count"])

        if fields:
            lines.append(format_line("mac_power", fields, tags=tags, timestamp_ns=int(ts * 1e9)))
    return lines


def load_all_powerlogs(powerlog_dir: Path, include_archives: bool = True) -> list[dict]:
    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="mac-telegraf-pl-") as tmpdir:
        tmp = Path(tmpdir)
        if include_archives:
            for gz_path in sorted(glob.glob(str(powerlog_dir / "Archives" / "*.PLSQL.gz"))):
                dst = tmp / Path(gz_path).name[:-3]
                with gzip.open(gz_path, "rb") as fin, open(dst, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                records.extend(extract_powerlog_db(dst))

        cur_base = powerlog_dir / "CurrentPowerlog.PLSQL"
        if cur_base.exists():
            for ext in ("", "-wal", "-shm"):
                src = Path(f"{cur_base}{ext}")
                if src.exists():
                    shutil.copy2(src, tmp / f"current.sqlite{ext}")
            records.extend(extract_powerlog_db(tmp / "current.sqlite"))
    return records


def post_batches(
    lines: list[str],
    url: str,
    org: str,
    bucket: str,
    token: str,
    batch_size: int = 2000,
) -> int:
    endpoint = (
        f"{url.rstrip('/')}/api/v2/write?"
        + urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "ns"})
    )
    sent = 0
    for i in range(0, len(lines), batch_size):
        payload = ("\n".join(lines[i : i + batch_size]) + "\n").encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Token {token}",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Unexpected InfluxDB status {resp.status}")
        sent += min(batch_size, len(lines) - i)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--powerlog-dir", type=Path, default=DEFAULT_POWERLOG_DIR)
    parser.add_argument("--no-archives", action="store_true", help="Read CurrentPowerlog only")
    parser.add_argument("--host", default=os.environ.get("MAC_HOSTNAME") or socket.gethostname())
    parser.add_argument("--host-role", default=os.environ.get("MAC_HOST_ROLE", "laptop"))
    parser.add_argument("--dry-run", action="store_true", help="Print sample lines and count only")
    args = parser.parse_args()

    model = None
    try:
        model = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "hw.model"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except OSError:
        pass

    records = load_all_powerlogs(args.powerlog_dir, include_archives=not args.no_archives)
    lines = build_lines_from_records(records, host=args.host, host_role=args.host_role, model=model)
    if not lines:
        print("No Powerlog records found.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"Prepared {len(lines)} mac_power points (dry-run).")
        for sample in lines[:2] + (["..."] if len(lines) > 4 else []) + lines[-2:]:
            print(sample)
        return 0

    url = os.environ.get("INFLUXDB_URL")
    token = os.environ.get("INFLUXDB_TOKEN")
    org = os.environ.get("INFLUXDB_ORG")
    bucket = os.environ.get("INFLUXDB_BUCKET")
    if not all([url, token, org, bucket]):
        print(
            "Missing INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, or INFLUXDB_BUCKET.",
            file=sys.stderr,
        )
        return 2

    sent = post_batches(lines, url=url, org=org, bucket=bucket, token=token)
    print(f"Backfilled {sent} mac_power points to {url} (bucket={bucket}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
