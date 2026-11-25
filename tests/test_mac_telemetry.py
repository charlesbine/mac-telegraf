from __future__ import annotations

import plistlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mac_telemetry  # noqa: E402
import powerlog_backfill  # noqa: E402


class TestMacTelemetry(unittest.TestCase):
    def test_to_signed_int64(self) -> None:
        self.assertEqual(mac_telemetry.to_signed_int64(0), 0)
        self.assertEqual(mac_telemetry.to_signed_int64(1250), 1250)
        # -1500 in unsigned 64-bit two's complement
        self.assertEqual(mac_telemetry.to_signed_int64((1 << 64) - 1500), -1500)
        self.assertIsNone(mac_telemetry.to_signed_int64(None))

    def test_parse_smart_battery_with_accumulator_delta(self) -> None:
        sample = [
            {
                "ExternalConnected": True,
                "IsCharging": False,
                "FullyCharged": True,
                "CurrentCapacity": 100,
                "MaxCapacity": 100,
                "AppleRawCurrentCapacity": 6500,
                "AppleRawMaxCapacity": 6684,
                "DesignCapacity": 8694,
                "CycleCount": 569,
                "Temperature": 3079,
                "Voltage": 12543,
                "InstantAmperage": (1 << 64) - 420,
                "AdapterDetails": {"Watts": 65},
                "PowerTelemetryData": {
                    "SystemLoad": 15000,
                    "SystemPowerIn": 16000,
                    "AccumulatedSystemLoad": 1000000 + 25000 * 60,
                    "SystemLoadAccumulatorCount": 100 + 60,
                    "AccumulatedSystemPowerIn": 2000000 + 26500 * 60,
                    "SystemPowerInAccumulatorCount": 200 + 60,
                    "AdapterEfficiencyLoss": 300,
                    "SystemVoltageIn": 19842,
                    "SystemCurrentIn": 834,
                    "BatteryPower": 0,
                },
            }
        ]
        prev_state = {
            "ts": 1000.0,
            "acc_sys_load": 1000000,
            "acc_sys_load_cnt": 100,
            "acc_sys_pin": 2000000,
            "acc_sys_pin_cnt": 200,
        }
        fields, next_state = mac_telemetry.parse_smart_battery_plist(
            plistlib.dumps(sample), prev_state, now_ts=1060.0
        )

        # True interval average from accumulator delta, not instant 15W/16W
        self.assertEqual(fields["system_load_w"], 25.0)
        self.assertEqual(fields["system_load_inst_w"], 15.0)
        self.assertEqual(fields["system_power_in_w"], 26.5)
        self.assertEqual(fields["system_power_in_inst_w"], 16.0)
        self.assertEqual(fields["adapter_watts"], 65)
        self.assertEqual(fields["battery_temp_c"], 30.79)
        self.assertEqual(fields["battery_current_a"], -0.42)
        self.assertEqual(fields["cycle_count"], 569)
        self.assertAlmostEqual(float(fields["health_pct"]), 76.88, places=2)
        self.assertEqual(next_state["acc_sys_load_cnt"], 160)

    def test_parse_smart_battery_desktop_mac_empty(self) -> None:
        fields, next_state = mac_telemetry.parse_smart_battery_plist(
            plistlib.dumps([]), {}, now_ts=1000.0
        )
        self.assertEqual(fields, {})
        self.assertEqual(next_state, {})

    def test_parse_memory_pressure_and_swap(self) -> None:
        mp = "The system has 68719476736\nSystem-wide memory free percentage: 71%\n"
        self.assertEqual(mac_telemetry.parse_memory_pressure(mp), 71)

        swap = "total = 9216.00M  used = 1.50G  free = 512.00K  (encrypted)"
        parsed = mac_telemetry.parse_swap_usage(swap)
        self.assertEqual(parsed["swap_total_mb"], 9216.0)
        self.assertEqual(parsed["swap_used_mb"], 1536.0)
        self.assertEqual(parsed["swap_free_mb"], 0.5)

    def test_format_line_protocol(self) -> None:
        line = mac_telemetry.format_line(
            "mac_power",
            {"system_load_w": 25.5, "cycle_count": 569, "is_charging": 0},
            tags={"model": "MacBookPro18,2"},
            timestamp_ns=1700000000000000000,
        )
        self.assertEqual(
            line,
            "mac_power,model=MacBookPro18\\,2 cycle_count=569i,is_charging=0i,system_load_w=25.5 1700000000000000000",
        )

    def test_parse_gpu_plist(self) -> None:
        sample = [
            {
                "model": "Apple M1 Max",
                "gpu-core-count": 32,
                "PerformanceStatistics": {
                    "Device Utilization %": 42,
                    "Renderer Utilization %": 38,
                    "Tiler Utilization %": 12,
                    "Alloc system memory": 16 * 1024**3,
                    "In use system memory": 8 * 1024**3,
                    "In use system memory (driver)": 128 * 1024**2,
                },
            }
        ]
        fields, tags = mac_telemetry.parse_gpu_plist(
            plistlib.dumps(sample), total_mem_bytes=64 * 1024**3
        )
        self.assertEqual(tags["gpu_model"], "Apple M1 Max")
        self.assertEqual(fields["cores"], 32)
        self.assertEqual(fields["util_pct"], 42)
        self.assertEqual(fields["renderer_util_pct"], 38)
        self.assertEqual(fields["tiler_util_pct"], 12)
        self.assertEqual(fields["mem_alloc_mb"], 16384.0)
        self.assertEqual(fields["mem_in_use_mb"], 8192.0)
        self.assertEqual(fields["mem_alloc_pct"], 25.0)
        self.assertEqual(fields["mem_in_use_pct"], 12.5)

    def test_powerlog_backfill_lines(self) -> None:
        records = [
            {
                "ts": 1700000000.0,
                "sys_load_mw": 20000,
                "power_in_mw": 21000,
                "acc_load": 100000,
                "acc_load_cnt": 10,
                "acc_pin": 100000,
                "acc_pin_cnt": 10,
                "vin_mv": 20000,
                "iin_ma": 1050,
                "level": 99.0,
                "external_connected": True,
                "is_charging": False,
                "temp_raw": 3050,
                "cycle_count": 500,
            },
            {
                "ts": 1700000060.0,
                "sys_load_mw": 22000,
                "power_in_mw": 23000,
                "acc_load": 100000 + 30000 * 60,
                "acc_load_cnt": 70,
                "acc_pin": 100000 + 31000 * 60,
                "acc_pin_cnt": 70,
                "vin_mv": 20000,
                "iin_ma": 1150,
                "level": 100.0,
                "external_connected": True,
                "is_charging": False,
                "temp_raw": 3080,
                "cycle_count": 500,
            },
        ]
        lines = powerlog_backfill.build_lines_from_records(
            records, host="my-mac", host_role="laptop", model="MacBookPro18,2"
        )
        self.assertEqual(len(lines), 2)
        self.assertIn("system_load_w=30", lines[1])
        self.assertIn("system_power_in_w=31", lines[1])
        self.assertIn("source=powerlog", lines[1])


if __name__ == "__main__":
    unittest.main()
