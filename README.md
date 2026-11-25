# mac-telegraf

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/charlesbine/mac-telegraf/blob/main/LICENSE)
[![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon%20%26%20Intel-black.svg)](https://github.com/charlesbine/mac-telegraf)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://github.com/charlesbine/mac-telegraf)

Lightweight, no-`sudo` macOS telemetry setup for **Telegraf → InfluxDB v2 → Grafana**.

Ships standard host metrics (`cpu`, `mem`, `swap`, `disk`, `diskio`, `net`, `system`) alongside Apple Silicon GPU utilization & unified memory, hardware power draw, connected USB-C/MagSafe charger wattage, battery health/temperature, macOS memory pressure, and thermal state—with negligible battery overhead (~0.1% CPU at a 60s interval, zero external Python dependencies).

## Highlights

- **No `sudo`, no `powermetrics`:** Reads `ioreg` (`AppleSmartBattery` + `IOAccelerator`), `memory_pressure -Q`, `sysctl`, and `NSProcessInfo` directly.
- **True interval-averaged wattage:** Uses the SMC/PMU hardware accumulators (`AccumulatedSystemLoad`, `AccumulatedSystemPowerIn`) so a 60-second poll captures exact 60-second average watts rather than noisy point snapshots.
- **Laptop-friendly:** 60s interval, `Background` QoS + `LowPriorityIO` in `launchd`, and a 50,000-metric buffer that rides out sleep, lid closes, and offline travel.
- **Works on desktop Macs too:** Gracefully skips battery fields on Mac Studio / Mac mini while keeping CPU, memory pressure, swap, thermal state, disk, and network telemetry.
- **7-day historical backfill included:** Optional script to import macOS's built-in 1-minute Powerlog history (`/var/db/powerlog/Library/BatteryLife/`) straight into InfluxDB v2.

## Quickstart

### 1. Install Telegraf and clone

```bash
brew install telegraf
git clone https://github.com/charlesbine/mac-telegraf.git
cd mac-telegraf
```

### 2. Configure InfluxDB v2 credentials

```bash
cp .env.example .env
cp telegraf.conf.example telegraf.conf
```

Edit `.env` with your InfluxDB v2 URL, **bucket-scoped write token**, org, bucket, and host tags:

```dotenv
INFLUXDB_URL=https://influx.example.com
INFLUXDB_TOKEN=your-write-only-token
INFLUXDB_ORG=your-org
INFLUXDB_BUCKET=system_metrics
MAC_HOSTNAME=my-mac
MAC_HOST_ROLE=laptop
```

### 3. Test locally before enabling launchd

```bash
# Verify the Apple Silicon / macOS collector output
python3 scripts/mac_telemetry.py

# Dry-run Telegraf (collects once and prints line protocol, does not write)
set -a && source .env && set +a
MAC_TELEGRAF_DIR="$(pwd)" telegraf --config telegraf.conf --test
```

### 4. Install and run as a background LaunchAgent

```bash
./scripts/install.sh
```

`install.sh` copies the runtime files and `.env` (`0600`) to `~/.local/share/mac-telegraf` (avoiding macOS TCC restrictions on `~/Documents` or `~/Desktop`), detects GUI vs. headless `launchd` sessions automatically, and loads `com.charlesbine.mac-telegraf`.

Check logs at `/tmp/mac-telegraf.err.log`. To stop and unload:

```bash
launchctl bootout "gui/$(id -u)/com.charlesbine.mac-telegraf"
# (or user/$(id -u)/com.charlesbine.mac-telegraf on headless Macs)
```

## Optional: Backfill Historical Power Data

macOS already stores ~7 days of 1-minute PMU power telemetry in `/var/db/powerlog/Library/BatteryLife/`. Preview or backfill it into your InfluxDB bucket:

```bash
# Preview sample points without writing
python3 scripts/powerlog_backfill.py --dry-run

# Backfill into InfluxDB v2
set -a && source .env && set +a
python3 scripts/powerlog_backfill.py
```

## Grafana Dashboard

Import [`grafana/mac-telemetry-dashboard.json`](grafana/mac-telemetry-dashboard.json) in Grafana (**Dashboards → New → Import**), pick your InfluxDB v2 Flux datasource, set the `bucket` variable, and select your Mac from the `host` dropdown.

## Emitted Custom Metrics

In addition to Telegraf's built-in `cpu`, `mem`, `swap`, `system`, `processes`, `disk`, `diskio`, and `net` measurements, `scripts/mac_telemetry.py` emits:

| Measurement | Key Fields | Notes |
| --- | --- | --- |
| `mac_power` | `system_load_w`, `system_load_inst_w`, `system_power_in_w`, `system_power_in_inst_w`, `adapter_efficiency_loss_w`, `adapter_watts`, `input_voltage_v`, `input_current_a`, `battery_pct`, `battery_raw_pct`, `health_pct`, `cycle_count`, `battery_temp_c`, `battery_voltage_v`, `battery_current_a`, `battery_power_w`, `external_connected`, `is_charging`, `fully_charged` | Emitted on Macs with `AppleSmartBattery`; `*_w` fields use PMU accumulator deltas for exact interval averages |
| `mac_gpu` | `util_pct`, `renderer_util_pct`, `tiler_util_pct`, `mem_alloc_bytes`, `mem_alloc_mb`, `mem_alloc_pct`, `mem_in_use_bytes`, `mem_in_use_mb`, `mem_in_use_pct`, `mem_driver_bytes`, `cores` | Apple Silicon GPU utilization & unified memory from `IOAccelerator` (`PerformanceStatistics`), tagged with `gpu_model` |
| `mac_system` | `memory_free_pct`, `swap_used_mb`, `swap_total_mb`, `swap_free_mb`, `thermal_state`, `low_power_mode` | `thermal_state`: `0` Nominal, `1` Fair, `2` Serious, `3` Critical |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
