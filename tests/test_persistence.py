"""本地持久化：状态原子落盘，重启后完整恢复，重复事件跨重启仍幂等。"""

import tempfile
import unittest

from domain import (
    AuthScope,
    ConsentStatus,
    DataType,
    DeviceState,
    RuleStatus,
    SafetyLevel,
)
from household import STATE_FILE, Household


class PersistenceTest(unittest.TestCase):
    def test_state_file_created_on_seed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Household.seeded(data_dir=data_dir)
            loaded = Household(data_dir=data_dir)._store.load(STATE_FILE, None)
            self.assertIsNotNone(loaded)
            self.assertIn("rules", loaded)

    def test_full_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as data_dir:
            first = Household.seeded(data_dir=data_dir)
            # 制造一批各类状态变更
            first.handle(first.event(
                "motion_detected", device_id="corridor_sensor",
                occurred_at="2026-09-18T23:05:00", event_id="f1",
            ))
            first.rules.update(
                "sleep_house_dim",
                conditions={"time_range": {"start": "23:00", "end": "05:00"}},
            )
            first.consents.withdraw("consent-kid-sleep",
                                    withdrawn_at="2026-09-18T23:40:00")
            first.access.revoke("pass-cleaner")
            first.devices.begin_update("bedroom_ac", "6.0.0",
                                       at="2026-09-19T03:00:00")
            first.set_cloud(False)

            second = Household.bootstrap(data_dir)
            # 规则版本
            self.assertEqual(
                second.rules.active("sleep_house_dim").version, 2
            )
            self.assertEqual(
                second.rules.get_version("sleep_house_dim", 1).status,
                RuleStatus.SUPERSEDED,
            )
            # 同意撤回
            self.assertFalse(second.consents.permits(
                "kid", DataType.HEALTH, "sleep_adjust"))
            withdrawn = next(c for c in second.consents.all()
                             if c.id == "consent-kid-sleep")
            self.assertEqual(withdrawn.status, ConsentStatus.WITHDRAWN)
            # 访客撤销
            self.assertTrue(next(
                p for p in second.access.all() if p.id == "pass-cleaner"
            ).revoked)
            # 设备升级态
            self.assertEqual(second.devices.get("bedroom_ac").state,
                             DeviceState.UPDATING)
            # 云状态
            self.assertFalse(second.cloud_connected)
            # 审计记录
            self.assertEqual(len(second.audit.decisions), 1)

    def test_dedup_survives_restart(self):
        with tempfile.TemporaryDirectory() as data_dir:
            first = Household.seeded(data_dir=data_dir)
            event = first.event(
                "gas_leak", device_id="kitchen_gas",
                occurred_at="2026-09-19T02:00:00", event_id="f2",
                dedup_key="burst-restart",
                payload={"readings": {"kitchen_gas": {"gas_level": 30}}},
            )
            first.handle(event)
            executed_before = len(first.executor.actions)

            second = Household.bootstrap(data_dir)
            replay_event = second.event(
                "gas_leak", device_id="kitchen_gas",
                occurred_at="2026-09-19T02:00:00", event_id="f2",
                dedup_key="burst-restart",
                payload={"readings": {"kitchen_gas": {"gas_level": 30}}},
            )
            _, replayed = second.handle(replay_event)
            self.assertTrue(replayed)
            self.assertEqual(len(second.executor.actions), 0)
            self.assertEqual(executed_before, 1)

    def test_bootstrap_is_idempotent(self):
        with tempfile.TemporaryDirectory() as data_dir:
            Household.seeded(data_dir=data_dir)
            home = Household.bootstrap(data_dir)
            self.assertEqual(len(home.rules.history_ids()), 8)

    def test_decisions_remain_explainable_after_restart(self):
        with tempfile.TemporaryDirectory() as data_dir:
            first = Household.seeded(data_dir=data_dir)
            decision, _ = first.handle(first.event(
                "motion_detected", device_id="corridor_sensor",
                occurred_at="2026-09-18T23:05:00", event_id="f3",
            ))
            second = Household.bootstrap(data_dir)
            projection = second.audit.explain(
                decision.id, "mom", second.rules, second.devices
            )
            self.assertIsNotNone(projection)
            own = next(r for r in projection["results"]
                       if r.get("rule_id") == "sleep_house_dim")
            self.assertEqual(own["rule_version"], 1)


if __name__ == "__main__":
    unittest.main()
