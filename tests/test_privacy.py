"""同意与隐私门：目的限定、全员同意、撤回即时生效。"""

import unittest

from home import (
    ConsentGate,
    ConsentStatus,
    DeviceCapability,
    Event,
    LocalStore,
    PrivacyCategory,
)
from home.privacy import ConsentViolation


class ConsentGateTest(unittest.TestCase):
    def setUp(self):
        self.gate = ConsentGate(LocalStore(None))

    def test_environment_data_needs_no_consent(self):
        self.gate.authorize_read(DeviceCapability.LIGHT, [], None)  # 不抛异常
        self.gate.authorize_read(DeviceCapability.CLIMATE, [], None)

    def test_camera_requires_purpose(self):
        with self.assertRaises(ConsentViolation):
            self.gate.authorize_read(DeviceCapability.CAMERA_STREAM, ["m_elder"], None)

    def test_camera_requires_grant_for_each_subject(self):
        self.gate.grant("m_elder", PrivacyCategory.IMAGE, "guest_verification")
        self.gate.authorize_read(DeviceCapability.CAMERA_STREAM,
                                 ["m_elder"], "guest_verification")
        # 第二主体未授权 -> 拒绝
        with self.assertRaises(ConsentViolation):
            self.gate.authorize_read(
                DeviceCapability.CAMERA_STREAM, ["m_elder", "m_kid"],
                "guest_verification")

    def test_purpose_is_scoped(self):
        self.gate.grant("m_elder", PrivacyCategory.IMAGE, "guest_verification")
        with self.assertRaises(ConsentViolation):
            self.gate.authorize_read(
                DeviceCapability.CAMERA_STREAM, ["m_elder"], "night_safety")

    def test_withdrawal_stops_new_use_immediately(self):
        self.gate.grant("m_elder", PrivacyCategory.HEALTH, "health_monitoring")
        self.assertTrue(self.gate.is_granted(
            "m_elder", PrivacyCategory.HEALTH, "health_monitoring"))
        self.gate.withdraw("m_elder", PrivacyCategory.HEALTH, "health_monitoring")
        with self.assertRaises(ConsentViolation):
            self.gate.authorize_read(
                DeviceCapability.SLEEP_MONITOR, ["m_elder"], "health_monitoring")
        self.assertEqual(
            self.gate.list_for("m_elder")[0].status, ConsentStatus.WITHDRAWN)

    def test_withdraw_all_for_subject(self):
        self.gate.grant("m_elder", PrivacyCategory.IMAGE, "a")
        self.gate.grant("m_elder", PrivacyCategory.IMAGE, "b")
        n = self.gate.withdraw_all("m_elder")
        self.assertEqual(n, 2)
        self.assertFalse(self.gate.is_granted("m_elder", PrivacyCategory.IMAGE, "a"))

    def test_event_gate_uses_event_purpose(self):
        self.gate.grant("m_elder", PrivacyCategory.IMAGE, "guest_verification")
        event = Event.make("visitor_arrival", purpose="guest_verification")
        self.gate.authorize_event(
            event, DeviceCapability.CAMERA_STREAM, ["m_elder"])


if __name__ == "__main__":
    unittest.main()
