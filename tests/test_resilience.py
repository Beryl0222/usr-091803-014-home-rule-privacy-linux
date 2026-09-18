"""降级与恢复：离线、固件升级、缺传感器的兜底与恢复重放。"""

import unittest

from home import (
    Device,
    DeviceCapability,
    DeviceStatus,
)
from home.resilience import DegradationReason, DeviceRegistry, ResiliencePolicy


class ResiliencePolicyTest(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceRegistry()
        self.policy = ResiliencePolicy()
        self.lock = Device("d_lock", "门锁", frozenset({DeviceCapability.LOCK}))
        self.light = Device("d_light", "夜灯", frozenset({DeviceCapability.LIGHT}),
                            requires_sensors=frozenset({"presence"}),
                            safety_critical=True)
        self.ac = Device("d_ac", "空调",
                         frozenset({DeviceCapability.CLIMATE, DeviceCapability.POWER}),
                         requires_sensors=frozenset({"temp"}))
        for d in (self.lock, self.light, self.ac):
            self.registry.register(d)

    def evaluate(self, device, command, event_sensors=frozenset()):
        return self.policy.evaluate(
            device, self.registry.state(device.device_id), command, event_sensors)

    def test_online_with_sensors_executes(self):
        self.registry.set_sensors("d_ac", frozenset({"temp"}))
        self.assertIsNone(self.evaluate(self.ac, "set", frozenset()))

    def test_offline_blocks_and_waits(self):
        self.registry.set_status("d_lock", DeviceStatus.OFFLINE)
        d = self.evaluate(self.lock, "unlock")
        self.assertEqual(d.reason, DegradationReason.OFFLINE)
        self.assertIsNone(d.fallback_command)
        self.assertTrue(d.retry_on_recovery)

    def test_upgrading_blocks_unlock(self):
        self.registry.set_status("d_lock", DeviceStatus.UPGRADING)
        d = self.evaluate(self.lock, "unlock")
        self.assertEqual(d.reason, DegradationReason.UPGRADING)
        self.assertIsNone(d.fallback_command)

    def test_missing_sensor_keeps_door_locked(self):
        # 门锁即使在缺信息时也不能被打开
        d = self.evaluate(self.lock, "unlock")
        # 锁本身不声明传感器需求，这里通过给锁加需求再测
        lock2 = Device("d_lock2", "门锁2", frozenset({DeviceCapability.LOCK}),
                       requires_sensors=frozenset({"presence"}))
        self.registry.register(lock2)
        d = self.evaluate(lock2, "unlock")
        self.assertEqual(d.reason, DegradationReason.MISSING_SENSOR)
        self.assertEqual(d.fallback_command, "lock")

    def test_missing_sensor_keeps_nightlight_on(self):
        d = self.evaluate(self.light, "on")
        self.assertEqual(d.reason, DegradationReason.MISSING_SENSOR)
        self.assertEqual(d.fallback_command, "on")

    def test_missing_sensor_does_not_cut_power(self):
        d = self.evaluate(self.ac, "off")
        self.assertEqual(d.reason, DegradationReason.MISSING_SENSOR)
        self.assertIsNone(d.fallback_command)

    def test_event_sensor_can_satisfy_requirement(self):
        # 设备没有固定传感器，但事件携带了所需读数
        self.assertIsNone(self.evaluate(self.ac, "set", frozenset({"temp"})))


if __name__ == "__main__":
    unittest.main()
