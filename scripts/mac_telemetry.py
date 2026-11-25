#!/usr/bin/env python3
"""No-sudo macOS / Apple Silicon telemetry collector for Telegraf (Influx Line Protocol).

Collects hardware power, battery health, connected USB-C/MagSafe charger wattage,
memory pressure, swap usage, thermal state, and Low Power Mode using only the Python
standard library and built-in macOS interfaces (ioreg, memory_pressure, sysctl,
NSProcessInfo).

Works on both MacBooks (full power + battery metrics) and desktop Macs such as
Mac Studio / Mac mini (emits mac_system metrics and skips battery metrics cleanly).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

INT64_MAX = (1 << 63) - 1
UINT64_MOD = 1 << 64


def to_signed_int64(value: int | None) -> int | None:
    """Convert unsigned 64-bit two's-complement integers returned by ioreg to signed."""
    if value is None:
        return None
    if value > INT64_MAX:
        return value - UINT64_MOD
    return value


def escape_tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def format_field_value(value: int | float | bool | str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if "." in f"{value:.4f}" else f"{value:.4f}"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_line(
    measurement: str,
    fields: dict[str, int | float | bool | str],
    tags: dict[str, str] | None = None,
    timestamp_ns: int | None = None,
) -> str:
    """Format a single InfluxDB Line Protocol record."""
    if not fields:
        return ""
    header = measurement
    if tags:
        tag_str = ",".join(f"{escape_tag(k)}={escape_tag(v)}" for k, v in sorted(tags.items()) if v)
        if tag_str:
            header = f"{header},{tag_str}"
    field_str = ",".join(f"{k}={format_field_value(v)}" for k, v in sorted(fields.items()))
    if timestamp_ns is not None:
        return f"{header} {field_str} {timestamp_ns}"
    return f"{header} {field_str}"


def default_state_path() -> Path:
    env_path = os.environ.get("MAC_TELEMETRY_STATE_FILE")
    if env_path:
        return Path(env_path)
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "mac-telegraf" / "state.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except Exception:
            os.unlink(tmp)
            raise
    except OSError:
        # Non-fatal: instantaneous readings still work without persisted accumulator state.
        pass


def compute_accumulator_avg_watts(
    curr_acc: int | None,
    curr_cnt: int | None,
    prev_acc: int | None,
    prev_cnt: int | None,
    fallback_mw: int | None,
    max_plausible_w: float = 250.0,
) -> float | None:
    """Compute true interval-averaged watts from PMU hardware accumulator deltas."""
    if (
        curr_acc is not None
        and curr_cnt is not None
        and prev_acc is not None
        and prev_cnt is not None
    ):
        d_cnt = curr_cnt - prev_cnt
        d_acc = curr_acc - prev_acc
        if d_cnt > 0 and d_acc >= 0:
            avg_w = (d_acc / d_cnt) / 1000.0
            if 0.0 <= avg_w <= max_plausible_w:
                return round(avg_w, 3)
    if fallback_mw is not None and fallback_mw >= 0:
        return round(fallback_mw / 1000.0, 3)
    return None


def parse_smart_battery_plist(
    raw_plist: bytes,
    prev_state: dict[str, Any],
    now_ts: float | None = None,
) -> tuple[dict[str, int | float | bool | str], dict[str, Any]]:
    """Parse `ioreg -arn AppleSmartBattery` XML plist into Influx fields + next state."""
    if not raw_plist.strip():
        return {}, prev_state

    entries = plistlib.loads(raw_plist)
    if not entries or not isinstance(entries, list):
        return {}, prev_state

    b: dict[str, Any] = entries[0]
    pt: dict[str, Any] = b.get("PowerTelemetryData") or {}
    ad: dict[str, Any] = b.get("AdapterDetails") or {}

    now = now_ts if now_ts is not None else time.time()
    prev_ts = prev_state.get("ts")
    # Ignore stale state across very long sleep/reboot gaps (> 15 min) or counter resets.
    state_fresh = isinstance(prev_ts, (int, float)) and 0 < (now - prev_ts) <= 900

    prev_load_acc = prev_state.get("acc_sys_load") if state_fresh else None
    prev_load_cnt = prev_state.get("acc_sys_load_cnt") if state_fresh else None
    prev_pin_acc = prev_state.get("acc_sys_pin") if state_fresh else None
    prev_pin_cnt = prev_state.get("acc_sys_pin_cnt") if state_fresh else None

    sys_load_mw = pt.get("SystemLoad")
    sys_pin_mw = pt.get("SystemPowerIn")
    acc_load = pt.get("AccumulatedSystemLoad")
    acc_load_cnt = pt.get("SystemLoadAccumulatorCount")
    acc_pin = pt.get("AccumulatedSystemPowerIn")
    acc_pin_cnt = pt.get("SystemPowerInAccumulatorCount")

    sys_load_w = compute_accumulator_avg_watts(
        acc_load, acc_load_cnt, prev_load_acc, prev_load_cnt, sys_load_mw
    )
    sys_pin_w = compute_accumulator_avg_watts(
        acc_pin, acc_pin_cnt, prev_pin_acc, prev_pin_cnt, sys_pin_mw
    )

    fields: dict[str, int | float | bool | str] = {}
    has_battery = bool(b.get("BatteryInstalled", b.get("DesignCapacity") is not None))

    if has_battery:
        if sys_load_w is not None:
            fields["system_load_w"] = sys_load_w
        if sys_load_mw is not None:
            fields["system_load_inst_w"] = round(sys_load_mw / 1000.0, 3)
        if sys_pin_w is not None:
            fields["system_power_in_w"] = sys_pin_w
        if sys_pin_mw is not None:
            fields["system_power_in_inst_w"] = round(sys_pin_mw / 1000.0, 3)
        if pt.get("AdapterEfficiencyLoss") is not None:
            fields["adapter_efficiency_loss_w"] = round(pt["AdapterEfficiencyLoss"] / 1000.0, 3)

    vin_mv = pt.get("SystemVoltageIn")
    iin_ma = pt.get("SystemCurrentIn")
    if vin_mv is not None and vin_mv > 0:
        fields["input_voltage_v"] = round(vin_mv / 1000.0, 3)
    if iin_ma is not None and iin_ma >= 0:
        fields["input_current_a"] = round(iin_ma / 1000.0, 3)
    if vin_mv is not None and iin_ma is not None and vin_mv > 0 and iin_ma >= 0:
        input_power_w = round((vin_mv / 1000.0) * (iin_ma / 1000.0), 3)
        fields["input_power_w"] = input_power_w
        if not has_battery:
            # Desktop Macs (e.g. Mac Studio) report 0 for SystemLoad/SystemPowerIn
            # while exposing real 12V rail voltage & current in PowerTelemetryData.
            fields["system_power_in_w"] = input_power_w

    if has_battery:
        batt_power_mw = to_signed_int64(pt.get("BatteryPower"))
        if batt_power_mw is not None:
            fields["battery_power_w"] = round(batt_power_mw / 1000.0, 3)

        external_connected = bool(b.get("ExternalConnected", False))
        is_charging = bool(b.get("IsCharging", False))
        fully_charged = bool(b.get("FullyCharged", False))
        fields["external_connected"] = 1 if external_connected else 0
        fields["is_charging"] = 1 if is_charging else 0
        fields["fully_charged"] = 1 if fully_charged else 0

        adapter_watts = int(ad["Watts"]) if external_connected and ad.get("Watts") is not None else 0
        fields["adapter_watts"] = adapter_watts

        current_cap = b.get("CurrentCapacity")
        max_cap = b.get("MaxCapacity")
        if current_cap is not None and max_cap:
            fields["battery_pct"] = round((current_cap / max_cap) * 100.0, 2)

        raw_curr = b.get("AppleRawCurrentCapacity")
        raw_max = b.get("AppleRawMaxCapacity")
        design_cap = b.get("DesignCapacity")
        if raw_curr is not None:
            fields["current_capacity_mah"] = int(raw_curr)
        if raw_max is not None:
            fields["max_capacity_mah"] = int(raw_max)
        if design_cap is not None:
            fields["design_capacity_mah"] = int(design_cap)
        if raw_curr is not None and raw_max:
            fields["battery_raw_pct"] = round((raw_curr / raw_max) * 100.0, 2)
        if raw_max is not None and design_cap:
            fields["health_pct"] = round((raw_max / design_cap) * 100.0, 2)

        if b.get("CycleCount") is not None:
            fields["cycle_count"] = int(b["CycleCount"])
        if b.get("Temperature") is not None:
            fields["battery_temp_c"] = round(b["Temperature"] / 100.0, 2)
        if b.get("Voltage") is not None:
            fields["battery_voltage_v"] = round(b["Voltage"] / 1000.0, 3)

        inst_amp = to_signed_int64(b.get("InstantAmperage"))
        if inst_amp is not None:
            fields["battery_current_a"] = round(inst_amp / 1000.0, 3)

    next_state = {
        "ts": now,
        "acc_sys_load": acc_load,
        "acc_sys_load_cnt": acc_load_cnt,
        "acc_sys_pin": acc_pin,
        "acc_sys_pin_cnt": acc_pin_cnt,
    }
    return fields, next_state


def read_smart_battery(state_path: Path) -> dict[str, int | float | bool | str]:
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/ioreg", "-arn", "AppleSmartBattery"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    prev_state = load_state(state_path)
    fields, next_state = parse_smart_battery_plist(raw, prev_state)
    if fields:
        save_state(state_path, next_state)
    return fields


def parse_memory_pressure(output: str) -> int | None:
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
    return int(match.group(1)) if match else None


def parse_swap_usage(output: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("total", "used", "free"):
        match = re.search(rf"{key}\s*=\s*([0-9.]+)([KMG])", output)
        if not match:
            continue
        val = float(match.group(1))
        unit = match.group(2)
        if unit == "K":
            val /= 1024.0
        elif unit == "G":
            val *= 1024.0
        out[f"swap_{key}_mb"] = round(val, 2)
    return out


def read_nsprocessinfo_power() -> dict[str, int]:
    """Read thermalState (0..3) and isLowPowerModeEnabled (0/1) directly via libobjc."""
    try:
        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            return {}
        objc = ctypes.cdll.LoadLibrary(objc_path)
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        msg_ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )

        cls = objc.objc_getClass(b"NSProcessInfo")
        if not cls:
            return {}
        proc = msg_ptr(cls, objc.sel_registerName(b"processInfo"))
        if not proc:
            return {}

        thermal = int(msg_long(proc, objc.sel_registerName(b"thermalState")))
        low_power = 1 if msg_bool(proc, objc.sel_registerName(b"isLowPowerModeEnabled")) else 0
        return {"thermal_state": thermal, "low_power_mode": low_power}
    except Exception:
        return {}


def read_mac_system() -> tuple[dict[str, int | float | bool | str], dict[str, str]]:
    fields: dict[str, int | float | bool | str] = {}
    tags: dict[str, str] = {}

    try:
        mp_out = subprocess.check_output(
            ["/usr/bin/memory_pressure", "-Q"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        free_pct = parse_memory_pressure(mp_out)
        if free_pct is not None:
            fields["memory_free_pct"] = free_pct
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        swap_out = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        fields.update(parse_swap_usage(swap_out))
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        model = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "hw.model"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        if model:
            tags["model"] = model
    except (OSError, subprocess.SubprocessError):
        pass

    fields.update(read_nsprocessinfo_power())
    return fields, tags


def parse_gpu_plist(
    raw_plist: bytes,
    total_mem_bytes: int | None = None,
) -> tuple[dict[str, int | float | bool | str], dict[str, str]]:
    """Parse `ioreg -arw0 -c IOAccelerator` XML plist into GPU util & memory fields."""
    if not raw_plist.strip():
        return {}, {}

    entries = plistlib.loads(raw_plist)
    if not entries or not isinstance(entries, list):
        return {}, {}

    # Prefer an accelerator entry that actually exposes PerformanceStatistics.
    accel: dict[str, Any] | None = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("PerformanceStatistics"):
            accel = entry
            break
    if accel is None:
        return {}, {}

    ps: dict[str, Any] = accel.get("PerformanceStatistics") or {}
    fields: dict[str, int | float | bool | str] = {}
    tags: dict[str, str] = {}

    gpu_model = accel.get("model")
    if isinstance(gpu_model, str) and gpu_model.strip():
        tags["gpu_model"] = gpu_model.strip()

    cores = accel.get("gpu-core-count")
    if isinstance(cores, int):
        fields["cores"] = cores

    if ps.get("Device Utilization %") is not None:
        fields["util_pct"] = int(ps["Device Utilization %"])
    if ps.get("Renderer Utilization %") is not None:
        fields["renderer_util_pct"] = int(ps["Renderer Utilization %"])
    if ps.get("Tiler Utilization %") is not None:
        fields["tiler_util_pct"] = int(ps["Tiler Utilization %"])

    alloc_bytes = ps.get("Alloc system memory")
    if isinstance(alloc_bytes, int) and alloc_bytes >= 0:
        fields["mem_alloc_bytes"] = alloc_bytes
        fields["mem_alloc_mb"] = round(alloc_bytes / (1024.0 * 1024.0), 2)
        if total_mem_bytes and total_mem_bytes > 0:
            fields["mem_alloc_pct"] = round((alloc_bytes / total_mem_bytes) * 100.0, 2)

    in_use_bytes = ps.get("In use system memory")
    if isinstance(in_use_bytes, int) and in_use_bytes >= 0:
        fields["mem_in_use_bytes"] = in_use_bytes
        fields["mem_in_use_mb"] = round(in_use_bytes / (1024.0 * 1024.0), 2)
        if total_mem_bytes and total_mem_bytes > 0:
            fields["mem_in_use_pct"] = round((in_use_bytes / total_mem_bytes) * 100.0, 2)

    driver_bytes = ps.get("In use system memory (driver)")
    if isinstance(driver_bytes, int) and driver_bytes >= 0:
        fields["mem_driver_bytes"] = driver_bytes

    return fields, tags


def read_total_mem_bytes() -> int | None:
    try:
        out = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read_gpu(total_mem_bytes: int | None) -> tuple[dict[str, int | float | bool | str], dict[str, str]]:
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/ioreg", "-arw0", "-c", "IOAccelerator"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    return parse_gpu_plist(raw, total_mem_bytes=total_mem_bytes)


def collect_lines(state_path: Path | None = None) -> list[str]:
    lines: list[str] = []
    sys_fields, sys_tags = read_mac_system()
    if sys_fields:
        lines.append(format_line("mac_system", sys_fields, tags=sys_tags))

    gpu_fields, gpu_tags = read_gpu(total_mem_bytes=read_total_mem_bytes())
    if gpu_fields:
        merged_gpu_tags = {**sys_tags, **gpu_tags}
        lines.append(format_line("mac_gpu", gpu_fields, tags=merged_gpu_tags))

    power_fields = read_smart_battery(state_path or default_state_path())
    if power_fields:
        lines.append(format_line("mac_power", power_fields, tags=sys_tags))

    return lines


def main() -> int:
    for line in collect_lines():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
