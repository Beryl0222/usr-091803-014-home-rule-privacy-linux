"""冲突仲裁：安全级别 > 授权范围 > 确立时间，唯一且可解释。"""

import unittest

from domain import AuthScope, Outcome, SafetyLevel
from household import Household


def by_rule(decision):
    return {(r.rule_id, r.rule_version): r for r in decision.results}


class ArbitrationTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def test_safety_beats_comfort_on_same_device(self):
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="t1",
        ))
        self.assertEqual(decision.winner_keys, ["elder_night_light:1"])
        rows = by_rule(decision)
        winner = rows[("elder_night_light", 1)]
        loser = rows[("sleep_house_dim", 1)]
        self.assertEqual(winner.outcome, Outcome.EXECUTED)
        self.assertEqual(winner.rank, 0)
        self.assertEqual(loser.outcome, Outcome.SUPPRESSED)
        self.assertEqual(loser.rank, 1)
        self.assertIn("lost_conflict", loser.reasons)

    def test_only_one_command_reaches_device(self):
        self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="t2",
        ))
        commands = [a["command"] for a in self.home.executor.actions]
        self.assertEqual(commands, ["on"])

    def test_visitor_scope_beats_cloud_default_for_lock(self):
        # 在线：访客 unlock（visitor 授权）胜过云端 lock（cloud_default 授权）
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="front_door_lock",
            occurred_at="2026-09-19T10:00:00", event_id="t3",
        ))
        self.assertEqual(decision.winner_keys, ["visitor_daytime_admit:1"])
        self.assertEqual(
            by_rule(decision)[("cloud_door_default", 1)].outcome,
            Outcome.SUPPRESSED,
        )

    def test_emergency_beats_everything_on_same_device(self):
        # 有人添加了同设备同事件的普通安全规则：紧急疏散仍唯一获胜
        self.home.rules.add(
            rule_id="gas_guard_lock", name="燃气时锁门防护（普通安全）",
            trigger="gas_leak", conditions={},
            device_id="front_door_lock", command="lock", params={},
            requester_id="dad", safety=SafetyLevel.SAFETY,
            scope=AuthScope.EXPLICIT_CONSENT,
            created_at="2026-09-10T20:00:00",
        )
        decision, _ = self.home.handle(self.home.event(
            "gas_leak", device_id="kitchen_gas",
            occurred_at="2026-09-19T10:00:00", event_id="t4",
            payload={"readings": {"kitchen_gas": {"gas_level": 40}}},
        ))
        self.assertEqual(decision.winner_keys, ["gas_leak_evacuate:1"])
        winner = decision.winner
        self.assertEqual(winner.safety, SafetyLevel.EMERGENCY)
        self.assertEqual(
            by_rule(decision)[("gas_guard_lock", 1)].outcome,
            Outcome.SUPPRESSED,
        )

    def test_tie_break_is_deterministic_by_creation_time(self):
        # 同安全级别同授权范围：确立更早的规则获胜
        self.home.rules.add(
            rule_id="late_rule", name="后来的同级别规则",
            trigger="motion_detected", conditions={},
            device_id="corridor_light", command="on", params={},
            requester_id="dad", safety=SafetyLevel.SAFETY,
            scope=AuthScope.EXPLICIT_CONSENT,
            created_at="2026-09-10T20:00:00",
        )
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="t5",
        ))
        self.assertEqual(decision.winner_keys, ["elder_night_light:1"])
        rows = by_rule(decision)
        self.assertEqual(rows[("late_rule", 1)].reasons, ("lost_conflict",))

    def test_different_devices_can_both_execute(self):
        # 夜灯（走廊灯）与睡眠联动（空调）设备不同，不应互相压制
        self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="t6",
        ))
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-18T23:06:00", event_id="t7",
            payload={"subject_id": "kid",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertEqual(decision.winner_keys, ["sleep_thermal_adjust:1"])
        commands = sorted((a["device_id"], a["command"])
                          for a in self.home.executor.actions)
        self.assertEqual(commands,
                         [("bedroom_ac", "set_mode"), ("corridor_light", "on")])

    def test_outside_time_window_candidates_do_not_compete(self):
        # 18:00 两条灯规则条件都不满足
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T18:00:00", event_id="t8",
        ))
        self.assertEqual(decision.winner_keys, [])
        self.assertTrue(
            all(r.outcome == Outcome.DENIED for r in decision.results)
        )


if __name__ == "__main__":
    unittest.main()
