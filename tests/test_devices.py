"""设备生命周期：固件升级、传感器缺失、离线的降级与恢复。"""

import unittest

from domain import DeviceState
from household import Household
from tests.test_arbitration import by_rule


class DeviceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def sleep_event(self, eid, at="2026-09-19T03:01:00", subject="grandpa"):
        return self.home.event(
            "sleep_reading", device_id="sleep_band", occurred_at=at,
            event_id=eid,
            payload={"subject_id": subject,
                     "readings": {"sleep_band": {"breathing": 10}}},
        )

    def test_firmware_update_blocks_non_local_capabilities(self):
        self.home.devices.begin_update("bedroom_ac", "6.0.0",
                                       at="2026-09-19T03:00:00")
        decision, _ = self.home.handle(self.sleep_event("d1"))
        self.assertEqual(
            self.home.devices.get("bedroom_ac").state,
            DeviceState.UPDATING,
        )
        self.assertIn("device_updating",
                      by_rule(decision)[("sleep_thermal_adjust", 1)].reasons)

    def test_failed_update_rolls_back_firmware(self):
        self.home.devices.begin_update("bedroom_ac", "6.0.0",
                                       at="2026-09-19T03:00:00")
        device = self.home.devices.update_failed(
            "bedroom_ac", at="2026-09-19T03:02:00"
        )
        self.assertEqual(device.firmware_version, "5.0.2")
        self.assertEqual(device.state, DeviceState.RECOVERING)

    def test_must_verify_after_update_before_normal_service(self):
        self.home.devices.begin_update("bedroom_ac", "6.0.0",
                                       at="2026-09-19T03:00:00")
        self.home.devices.update_finished("bedroom_ac", "6.0.0",
                                          at="2026-09-19T03:10:00")
        decision, _ = self.home.handle(self.sleep_event("d2", "2026-09-19T03:11:00"))
        self.assertIn("device_recovering_unverified",
                      by_rule(decision)[("sleep_thermal_adjust", 1)].reasons)
        self.home.devices.verify_capabilities(
            "bedroom_ac", ("temperature",), at="2026-09-19T03:12:00"
        )
        decision, _ = self.home.handle(self.sleep_event("d3", "2026-09-19T03:13:00"))
        self.assertEqual(decision.winner_keys, ["sleep_thermal_adjust:1"])
        self.assertEqual(self.home.devices.get("bedroom_ac").firmware_version,
                         "6.0.0")

    def test_missing_sensor_degrades_and_blocks_comfort_rule(self):
        self.home.devices.sensor_lost("sleep_band", "breathing")
        self.assertEqual(self.home.devices.get("sleep_band").state,
                         DeviceState.DEGRADED)
        decision, _ = self.home.handle(self.sleep_event("d4", "2026-09-19T03:20:00"))
        self.assertIn("condition_sensor_missing",
                      by_rule(decision)[("sleep_thermal_adjust", 1)].reasons)

    def test_sensor_restored_returns_to_normal(self):
        self.home.devices.sensor_lost("sleep_band", "breathing")
        self.home.devices.sensor_restored("sleep_band", "breathing",
                                          at="2026-09-19T03:25:00")
        self.assertEqual(self.home.devices.get("sleep_band").state,
                         DeviceState.NORMAL)

    def test_emergency_rule_fails_safe_with_degraded_sensor(self):
        # 传感器缺失时普通读取应未知；紧急动作按故障安全放行并记录原因
        self.home.devices.sensor_lost("kitchen_gas", "gas_level")
        decision, _ = self.home.handle(self.home.event(
            "gas_leak", device_id="kitchen_gas",
            occurred_at="2026-09-19T04:00:00", event_id="d5",
            payload={"readings": {"kitchen_gas": {"gas_level": 30}}},
        ))
        winner = decision.winner
        self.assertIsNotNone(winner)
        self.assertEqual(winner.rule_id, "gas_leak_evacuate")
        self.assertTrue(
            any(n.startswith("emergency_fail_safe") for n in winner.notes)
        )
        self.assertEqual(self.home.executor.actions[-1]["command"], "unlock")

    def test_emergency_does_not_failsafe_during_update_or_offline(self):
        self.home.devices.begin_update("front_door_lock", "9.0.0",
                                       at="2026-09-19T05:00:00")
        decision, _ = self.home.handle(self.home.event(
            "gas_leak", device_id="kitchen_gas",
            occurred_at="2026-09-19T05:01:00", event_id="d6",
            payload={"readings": {"kitchen_gas": {"gas_level": 50}}},
        ))
        self.assertEqual(decision.winner_keys, [])
        self.assertIn("device_updating",
                      by_rule(decision)[("gas_leak_evacuate", 1)].reasons)

    def test_illegal_transition_is_rejected(self):
        # 未在升级中的设备不能完成升级
        with self.assertRaises(Exception):
            self.home.devices.update_finished("bedroom_ac", "9.9.9")


if __name__ == "__main__":
    unittest.main()
