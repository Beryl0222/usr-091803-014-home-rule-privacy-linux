"""规则版本：每次修改都留不可变版本，非法规则被拒。"""

import unittest

from home import (
    Device,
    DeviceCapability,
    RuleAction,
    RuleBook,
    RuleConflictError,
    LocalStore,
    TimeWindow,
)


class RuleVersionTest(unittest.TestCase):
    def setUp(self):
        self.book = RuleBook(LocalStore(None))
        self.devices = {
            "d_cam": Device("d_cam", "摄像头",
                            frozenset({DeviceCapability.CAMERA_STREAM}),
                            privacy_category=None),
        }

    def test_create_records_version(self):
        rule = self.book.create("夜间关摄像", "m_dad", "night",
                                [RuleAction("d_cam", DeviceCapability.CAMERA_STREAM, "stop")],
                                self.devices, purpose="night_safety", safety_level=3)
        self.assertEqual(rule.version, 1)
        history = self.book.history(rule.rule_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["change"], "create")

    def test_modify_bumps_version_and_keeps_history(self):
        rule = self.book.create("r", "m_dad", "night",
                                [RuleAction("d_cam", DeviceCapability.CAMERA_STREAM, "stop")],
                                self.devices, purpose="p")
        updated = self.book.modify(rule.rule_id, "m_dad", self.devices, safety_level=9)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.safety_level, 9)
        changes = [h["change"] for h in self.book.history(rule.rule_id)]
        self.assertEqual(changes, ["create", "modify"])
        # 历史快照冻结在旧版本
        self.assertEqual(self.book.history(rule.rule_id)[0]["rule"]["version"], 1)

    def test_disable_delete_leave_versions(self):
        rule = self.book.create("r", "m_dad", "night",
                                [RuleAction("d_cam", DeviceCapability.CAMERA_STREAM, "stop")],
                                self.devices, purpose="p")
        self.book.set_enabled(rule.rule_id, "m_dad", False)
        self.book.delete(rule.rule_id, "m_dad")
        self.assertIsNone(self.book.get(rule.rule_id))
        changes = [h["change"] for h in self.book.history(rule.rule_id)]
        self.assertEqual(changes, ["create", "disable", "delete"])

    def test_unknown_device_rejected(self):
        with self.assertRaises(RuleConflictError):
            self.book.create("bad", "m_dad", "night",
                             [RuleAction("d_nope", DeviceCapability.LIGHT, "on")],
                             self.devices)

    def test_missing_capability_rejected(self):
        with self.assertRaises(RuleConflictError):
            self.book.create("bad", "m_dad", "night",
                             [RuleAction("d_cam", DeviceCapability.LOCK, "unlock")],
                             self.devices)

    def test_camera_stream_requires_purpose(self):
        with self.assertRaises(RuleConflictError):
            self.book.create("bad", "m_dad", "night",
                             [RuleAction("d_cam", DeviceCapability.CAMERA_STREAM, "stream")],
                             self.devices)

    def test_window_crosses_midnight(self):
        window = TimeWindow(22, 6)
        self.assertTrue(window.contains(23))
        self.assertTrue(window.contains(2))
        self.assertFalse(window.contains(12))


if __name__ == "__main__":
    unittest.main()
