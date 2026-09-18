"""审计可见性：能解释自己相关动作，但无法推断他人敏感活动。"""

import unittest

from home import (
    AuditQuery,
    Event,
)
from tests.fixtures import TS, build_home


class AuditVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.home, self.store, self.ctx = build_home()

    def _camera_stream_event(self, purpose="guest_verification"):
        # 父母以"访客核验"目的查看客厅摄像，画面中涉及奶奶（已授权该用途）
        return Event.make(
            "manual_command", ts=TS, hour=15, actor_id="m_dad",
            payload={"device_id": "d_cam", "capability": "stream",
                     "command": "stream", "subjects": ["m_elder"]},
            purpose=purpose,
        )

    def test_subject_sees_full_explanation(self):
        self.home.process_event(self._camera_stream_event())
        rows = self.home.audit.query(AuditQuery("m_elder"))
        camera = [r for r in rows if r["device_id"] == "d_cam"]
        self.assertTrue(camera)
        row = camera[0]
        self.assertEqual(row["view"], "full")
        self.assertIn("m_dad", row["actor_id"])
        self.assertIn("stream", row["explanation"])

    def test_direct_operator_sees_own_action_fully(self):
        self.home.process_event(self._camera_stream_event())
        rows = self.home.audit.query(AuditQuery("m_dad"))
        camera = [r for r in rows if r["device_id"] == "d_cam"]
        self.assertTrue(camera)
        self.assertEqual(camera[0]["view"], "full")

    def test_other_member_sees_only_redacted(self):
        self.home.process_event(self._camera_stream_event())
        # 妈妈是管理员但并非操作者，只能看到脱敏设备级记录
        rows = self.home.audit.query(AuditQuery("m_mom"))
        camera = [r for r in rows if r.get("capability") == "stream"]
        self.assertTrue(camera)
        row = camera[0]
        self.assertEqual(row["view"], "redacted")
        # 脱敏后看不到是谁看的、看的是哪个房间/哪位老人
        self.assertNotIn("actor_id", row)
        self.assertNotIn("params", row)
        self.assertNotIn("device_name", row)
        self.assertIsNone(row["device_id"])
        self.assertIn("摄像设备", row["capability_label"])

    def test_unrelated_member_cannot_see_record(self):
        self.home.process_event(self._camera_stream_event())
        rows = self.home.audit.query(AuditQuery("m_kid"))
        self.assertEqual(
            [r for r in rows if r.get("capability") == "stream"], [])

    def test_protective_camera_stop_does_not_expose_other_members(self):
        # 夜间自动停止摄像不读取任何人，不应把其他家人登记为可见主体
        from home import Event as _Event
        self.home.process_event(_Event.make(
            "night_window", ts=TS, hour=23,
            sensors_present=frozenset({"presence", "temp"})))
        # 孩子对夜间这一组动作（含摄像停止/上锁/照明）无任何可见记录
        self.assertEqual(self.home.audit.query(AuditQuery("m_kid")), [])

    def test_explain_unknown_or_unauthorized_returns_none(self):
        self.home.process_event(self._camera_stream_event())
        # 无权者拿到 entry_id 也无法确认记录是否存在
        all_rows = self.home.audit.query(AuditQuery("m_elder"))
        entry_id = [r for r in all_rows if r["device_id"] == "d_cam"][0]["entry_id"]
        self.assertIsNone(self.home.audit.explain(entry_id, "m_kid"))

    def test_subject_can_explain_why_action_happened(self):
        self.home.process_event(self._camera_stream_event())
        entry_id = self.home.audit.query(AuditQuery("m_elder"))
        entry_id = [r for r in entry_id if r["device_id"] == "d_cam"][0]["entry_id"]
        row = self.home.audit.explain(entry_id, "m_elder")
        self.assertIn("manual_command", row["explanation"])

    def test_non_sensitive_records_visible_to_admins(self):
        # 门锁通行属于 presence，管理员可看完整运维记录
        visitor_pass = self.home.issue_visitor_pass("访客", TS - 100, TS + 100)
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        rows = self.home.audit.query(AuditQuery("m_mom"))
        lock = [r for r in rows if r["device_id"] == "d_lock"]
        self.assertTrue(lock)
        self.assertEqual(lock[0]["view"], "full")

    def test_redaction_does_not_leak_health_subject(self):
        # 睡眠带健康数据：非操作者管理员只能看到"健康设备"级别，无法定位到奶奶
        event = Event.make(
            "manual_command", ts=TS, hour=2, actor_id="m_dad",
            payload={"device_id": "d_sleep", "capability": "sleep",
                     "command": "read", "subjects": ["m_elder"]},
            purpose="health_monitoring",
        )
        self.home.process_event(event)
        rows = self.home.audit.query(AuditQuery("m_mom"))
        health = [r for r in rows if r.get("category") == "health"]
        self.assertTrue(health)
        row = health[0]
        self.assertEqual(row["view"], "redacted")
        self.assertNotIn("m_elder", str(row))


if __name__ == "__main__":
    unittest.main()
