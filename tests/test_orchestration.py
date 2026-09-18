"""端到端：三代同堂夜间冲突、访客通行、幂等、断网本地可用。"""

import unittest

from home import (
    AuditQuery,
    ActionOutcome,
    DeviceStatus,
    Event,
    PrivacyCategory,
)
from tests.fixtures import TS, build_home


def commands(home):
    return {(c.device_id, c.command): c for c in home.executed}


class NightConflictTest(unittest.TestCase):
    def setUp(self):
        self.home, self.store, self.ctx = build_home()

    def test_night_window_resolves_to_unique_safe_decisions(self):
        event = Event.make("night_window", ts=TS, hour=23,
                           sensors_present=frozenset({"presence", "temp"}))
        result = self.home.process_event(event)
        self.assertFalse(result["deduped"])
        cmds = commands(self.home)
        # 老人夜灯亮（安全级别压过省电关灯）
        self.assertIn(("d_light", "on"), cmds)
        self.assertNotIn(("d_light", "off"), cmds)
        # 夜间自动上锁
        self.assertIn(("d_lock", "lock"), cmds)
        # 摄像停止查看（隐私保护，不需同意）
        self.assertIn(("d_cam", "stop"), cmds)

    def test_window_outside_hours_does_not_fire(self):
        event = Event.make("night_window", ts=TS, hour=12,
                           sensors_present=frozenset({"presence", "temp"}))
        self.home.process_event(event)
        self.assertEqual(commands(self.home), {})

    def test_losing_action_is_recorded_as_suppressed_with_reason(self):
        event = Event.make("night_window", ts=TS, hour=23,
                           sensors_present=frozenset({"presence", "temp"}))
        self.home.process_event(event)
        rows = self.home.audit.query(AuditQuery("m_dad"))
        suppressed = [r for r in rows if r["outcome"] == ActionOutcome.SUPPRESSED.value]
        self.assertTrue(suppressed)
        self.assertIn("安全级别", suppressed[0]["explanation"])


class VisitorPassTest(unittest.TestCase):
    def setUp(self):
        self.home, self.store, self.ctx = build_home()

    def test_valid_pass_unlocks_in_daytime(self):
        visitor_pass = self.home.issue_visitor_pass("钟点工", TS - 600, TS + 3600)
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        self.assertIn(("d_lock", "unlock"), commands(self.home))

    def test_expired_pass_denied(self):
        visitor_pass = self.home.issue_visitor_pass("旧访客", TS - 7200, TS - 10)
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        self.assertNotIn(("d_lock", "unlock"), commands(self.home))
        rows = self.home.audit.query(AuditQuery("m_dad"))
        self.assertTrue(any("有效期" in r["explanation"] for r in rows
                            if r["outcome"] == ActionOutcome.DENIED.value))

    def test_revoked_pass_denied(self):
        visitor_pass = self.home.issue_visitor_pass("被撤销者", TS - 100, TS + 100)
        self.home.revoke_visitor_pass(visitor_pass.pass_id)
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        self.assertNotIn(("d_lock", "unlock"), commands(self.home))

    def test_night_visitor_loses_to_standing_autolock_in_same_decision(self):
        # 夜间常驻上锁策略(安全级别2) 与 同一时刻的访客开锁(级别0)在一次仲裁中竞争
        visitor_pass = self.home.issue_visitor_pass("夜间访客", TS - 100, TS + 100)
        event = Event.make("visitor_unlock", ts=TS, hour=23, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        # 唯一决定：保持闭锁；访客开锁落败并留痕
        self.assertNotIn(("d_lock", "unlock"), commands(self.home))
        self.assertIn(("d_lock", "lock"), commands(self.home))
        rows = self.home.audit.query(AuditQuery("m_dad"))
        suppressed = [r for r in rows if r["outcome"] == ActionOutcome.SUPPRESSED.value]
        self.assertTrue(any("安全级别" in r["explanation"] for r in suppressed))

    def test_parent_verified_override_admits_night_visitor(self):
        # 父母通过摄像核验身份后，以更高安全级别的管理员指令放行
        visitor_pass = self.home.issue_visitor_pass("夜间访客", TS - 100, TS + 100)
        self.home.process_event(
            Event.make("visitor_unlock", ts=TS, hour=23, actor_id=visitor_pass.pass_id))
        self.assertNotIn(("d_lock", "unlock"), commands(self.home))
        override = Event.make(
            "manual_command", ts=TS + 5, hour=23, actor_id="m_dad",
            payload={"device_id": "d_lock", "capability": "lock",
                     "command": "unlock", "safety_level": 3})
        self.home.process_event(override)
        self.assertIn(("d_lock", "unlock"), commands(self.home))


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.home, self.store, self.ctx = build_home()

    def test_duplicate_event_does_not_unlock_again(self):
        visitor_pass = self.home.issue_visitor_pass("访客", TS - 100, TS + 100)
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        unlocks_first = [c for c in self.home.executed if c.command == "unlock"]
        self.assertEqual(len(unlocks_first), 1)
        # 同一事件重放（网络重试）
        result = self.home.process_event(event)
        self.assertTrue(result["deduped"])
        unlocks_second = [c for c in self.home.executed if c.command == "unlock"]
        self.assertEqual(len(unlocks_second), 1)

    def test_duplicate_power_event_does_not_cut_power_again(self):
        event = Event.make("manual_command", ts=TS, hour=12, actor_id="m_dad",
                           payload={"device_id": "d_ac", "capability": "power",
                                    "command": "off"})
        self.home.process_event(event)
        again = self.home.process_event(event)
        self.assertTrue(again["deduped"])
        self.assertEqual(len([c for c in self.home.executed if c.device_id == "d_ac"]), 1)


class DegradationRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.home, self.store, self.ctx = build_home()

    def test_offline_lock_is_pending_and_replays_on_recovery(self):
        self.home.devices.set_status("d_lock", DeviceStatus.OFFLINE)
        event = Event.make("night_window", ts=TS, hour=23,
                           sensors_present=frozenset({"presence", "temp"}))
        result = self.home.process_event(event)
        self.assertIn("d_lock", result["pending_devices"])
        self.assertNotIn(("d_lock", "lock"), commands(self.home))
        self.assertEqual(self.home.pending_count(), 1)

        # 门锁恢复在线：重放挂起事件，这次真正上锁
        recovered = self.home.recover_device("d_lock")
        self.assertTrue(recovered["entry_ids"])
        self.assertIn(("d_lock", "lock"), commands(self.home))
        self.assertEqual(self.home.pending_count(), 0)

    def test_upgrading_lock_does_not_unlock(self):
        self.home.devices.set_status("d_lock", DeviceStatus.UPGRADING)
        visitor_pass = self.home.issue_visitor_pass("访客", TS - 100, TS + 100)
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=visitor_pass.pass_id)
        self.home.process_event(event)
        self.assertNotIn(("d_lock", "unlock"), commands(self.home))

    def test_missing_sensor_keeps_nightlight_on_as_fallback(self):
        # 让传感器缺失
        self.home.devices.set_sensors("d_light", frozenset())
        event = Event.make("night_window", ts=TS, hour=23,
                           sensors_present=frozenset({"temp"}))
        self.home.process_event(event)
        light_cmds = [c for c in self.home.executed if c.device_id == "d_light"]
        self.assertTrue(light_cmds)
        self.assertEqual(light_cmds[0].command, "on")
        self.assertTrue(light_cmds[0].fallback)

    def test_recovery_does_not_double_execute(self):
        self.home.devices.set_status("d_lock", DeviceStatus.OFFLINE)
        event = Event.make("night_window", ts=TS, hour=23,
                           sensors_present=frozenset({"presence", "temp"}))
        self.home.process_event(event)
        self.home.recover_device("d_lock")
        self.home.recover_device("d_lock")  # 再次恢复，队列已空
        locks = [c for c in self.home.executed
                 if c.device_id == "d_lock" and c.command == "lock"]
        self.assertEqual(len(locks), 1)


class ConsentWithdrawalFlowTest(unittest.TestCase):
    def setUp(self):
        self.home, self.store, self.ctx = build_home()

    def _stream(self):
        return Event.make(
            "manual_command", ts=TS, hour=15, actor_id="m_dad",
            payload={"device_id": "d_cam", "capability": "stream",
                     "command": "stream", "subjects": ["m_elder"]},
            purpose="guest_verification",
        )

    def test_stream_allowed_then_denied_after_withdrawal(self):
        self.home.process_event(self._stream())
        self.assertIn(("d_cam", "stream"), commands(self.home))
        # 奶奶撤回"访客核验"用途的摄像同意
        self.home.gate.withdraw("m_elder", PrivacyCategory.IMAGE, "guest_verification")
        self.home.process_event(self._stream())
        rows = self.home.audit.query(AuditQuery("m_elder"))
        denied = [r for r in rows if r["outcome"] == ActionOutcome.DENIED.value]
        self.assertTrue(any("隐私门" in r["explanation"] for r in denied))
        # 只有撤回前那一次真正推流
        self.assertEqual(
            len([c for c in self.home.executed
                 if c.device_id == "d_cam" and c.command == "stream"]),
            1)

    def test_protective_camera_stop_still_runs_after_withdrawal(self):
        # 撤回同意不应削弱隐私保护：夜间停止查看依旧执行
        self.home.gate.withdraw("m_elder", PrivacyCategory.IMAGE, "night_safety")
        event = Event.make("night_window", ts=TS, hour=23,
                           sensors_present=frozenset({"presence", "temp"}))
        self.home.process_event(event)
        self.assertIn(("d_cam", "stop"), commands(self.home))


class LocalOnlyTest(unittest.TestCase):
    def test_works_without_network_using_persisted_state(self):
        # 第一次运行写入本地文件；用文件重建一个全新编排器（模拟断网重启）
        home, store, ctx = build_home()
        path = store._path
        visitor_pass = home.issue_visitor_pass("访客", TS - 100, TS + 100)
        pass_id = visitor_pass.pass_id

        from home import HomeOrchestrator, LocalStore
        rebuilt = HomeOrchestrator(LocalStore(path))
        # 设备、成员、同意、规则、访客凭证全部来自本地
        self.assertEqual(len(rebuilt.members()), 4)
        self.assertIsNotNone(rebuilt.devices.get("d_lock"))
        self.assertTrue(rebuilt.gate.is_granted(
            "m_elder", __import__("home").PrivacyCategory.IMAGE, "night_safety"))
        event = Event.make("visitor_unlock", ts=TS, hour=15, actor_id=pass_id)
        rebuilt.process_event(event)
        self.assertIn(("d_lock", "unlock"), commands(rebuilt))


if __name__ == "__main__":
    unittest.main()
