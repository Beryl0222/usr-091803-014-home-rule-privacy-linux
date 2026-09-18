"""规则版本：每次修改/禁用留版本，旧版本可复盘，无变化不产生新版本。"""

import unittest

from domain import (
    AuthScope,
    Outcome,
    RuleStatus,
    SafetyLevel,
)
from household import Household
from tests.test_arbitration import by_rule


class RuleVersioningTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def test_update_creates_new_version_and_supersedes_old(self):
        old = self.home.rules.get_version("sleep_house_dim", 1)
        new = self.home.rules.update(
            "sleep_house_dim",
            conditions={"time_range": {"start": "23:00", "end": "05:00"}},
        )
        self.assertEqual(new.version, 2)
        self.assertEqual(new.parent_version, 1)
        self.assertEqual(old.status, RuleStatus.SUPERSEDED)
        self.assertEqual(self.home.rules.active("sleep_house_dim").version, 2)

    def test_no_change_update_is_idempotent(self):
        active = self.home.rules.active("sleep_house_dim")
        again = self.home.rules.update(
            "sleep_house_dim",
            conditions={"time_range": {"start": "22:30", "end": "05:30"}},
        )
        self.assertEqual(again.version, active.version)

    def test_past_decision_replays_against_recorded_version(self):
        # 22:45 旧版关灯（22:30 起）会与夜灯竞争
        decision_before, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T22:45:00", event_id="r1",
        ))
        self.assertEqual(
            by_rule(decision_before)[("sleep_house_dim", 1)].outcome,
            Outcome.SUPPRESSED,
        )
        # 修改为 23:00 起
        self.home.rules.update(
            "sleep_house_dim",
            conditions={"time_range": {"start": "23:00", "end": "05:00"}},
        )
        decision_after, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T22:45:00", event_id="r2",
        ))
        # 新版本不再匹配 22:45
        self.assertEqual(decision_after.winner_keys,
                         ["elder_night_light:1"])
        self.assertEqual(
            by_rule(decision_after)[("sleep_house_dim", 2)].outcome,
            Outcome.DENIED,
        )
        self.assertIn(
            "condition_unmet",
            by_rule(decision_after)[("sleep_house_dim", 2)].reasons,
        )
        # 旧版本仍可取出复盘
        v1 = self.home.rules.get_version("sleep_house_dim", 1)
        self.assertEqual(v1.status, RuleStatus.SUPERSEDED)
        self.assertEqual(v1.conditions["time_range"]["start"], "22:30")

    def test_disabled_rule_is_versioned_and_stops_matching(self):
        disabled = self.home.rules.disable("sleep_house_dim")
        self.assertEqual(disabled.status, RuleStatus.DISABLED)
        self.assertIsNone(self.home.rules.active("sleep_house_dim"))
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="r3",
        ))
        self.assertEqual(decision.winner_keys, ["elder_night_light:1"])
        self.assertNotIn(("sleep_house_dim", disabled.version),
                         by_rule(decision))

    def test_history_is_complete_and_ordered(self):
        self.home.rules.update(
            "sleep_house_dim", name="睡眠关灯v2",
            conditions={"time_range": {"start": "23:00", "end": "05:00"}},
        )
        self.home.rules.update("sleep_house_dim", name="睡眠关灯v3")
        history = self.home.rules.history("sleep_house_dim")
        self.assertEqual([r.version for r in history], [1, 2, 3])
        statuses = [r.status for r in history]
        self.assertEqual(statuses[:2],
                         [RuleStatus.SUPERSEDED, RuleStatus.SUPERSEDED])
        self.assertEqual(statuses[2], RuleStatus.ACTIVE)

    def test_safety_and_scope_changes_flow_into_new_version(self):
        new = self.home.rules.update(
            "sleep_house_dim", safety=SafetyLevel.EMERGENCY,
            scope=AuthScope.EMERGENCY,
        )
        self.assertEqual(new.safety, SafetyLevel.EMERGENCY)
        self.assertEqual(new.scope, AuthScope.EMERGENCY)

    def test_unknown_rule_update_raises(self):
        with self.assertRaises(KeyError):
            self.home.rules.update("does_not_exist", name="x")


if __name__ == "__main__":
    unittest.main()
