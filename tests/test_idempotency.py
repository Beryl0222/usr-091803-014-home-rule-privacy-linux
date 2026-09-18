"""幂等：重复事件不得再次开锁或断电。"""

import unittest

from domain import AuthScope, SafetyLevel
from household import Household


class EventDedupTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def test_same_dedup_key_does_not_unlock_twice(self):
        event = self.home.event(
            "gas_leak", device_id="kitchen_gas",
            occurred_at="2026-09-19T02:00:00", event_id="i1",
            dedup_key="sensor-burst-1",
            payload={"readings": {"kitchen_gas": {"gas_level": 30}}},
        )
        first, replayed1 = self.home.handle(event)
        self.assertFalse(replayed1)
        second, replayed2 = self.home.handle(event)
        self.assertTrue(replayed2)
        self.assertIs(first, second)
        unlocks = [a for a in self.home.executor.actions
                   if a["command"] == "unlock"]
        self.assertEqual(len(unlocks), 1)

    def test_redelivery_with_same_event_id_is_replay(self):
        # 消息总线重发同一事件：event_id 相同即视为同一事件
        def build(eid):
            return self.home.event(
                "motion_detected", device_id="corridor_sensor",
                occurred_at="2026-09-18T23:05:00", event_id=eid,
            )
        d1, r1 = self.home.handle(build("evt-stable-1"))
        d2, r2 = self.home.handle(build("evt-stable-1"))
        self.assertFalse(r1)
        self.assertTrue(r2)
        self.assertEqual(d1.id, d2.id)
        self.assertEqual(len(self.home.executor.actions), 1)

    def test_distinct_event_ids_are_distinct_events(self):
        e1 = self.home.event("motion_detected", device_id="corridor_sensor",
                             occurred_at="2026-09-18T23:05:00", event_id="i2")
        e2 = self.home.event("motion_detected", device_id="corridor_sensor",
                             occurred_at="2026-09-18T23:05:00", event_id="i3")
        self.assertNotEqual(self.home._orchestrator.fingerprint(e1),
                            self.home._orchestrator.fingerprint(e2))
        self.home.handle(e1)
        _, replayed = self.home.handle(e2)
        self.assertFalse(replayed)
        self.assertEqual(len(self.home.executor.actions), 2)

    def test_different_minute_events_are_distinct(self):
        e1 = self.home.event("motion_detected", device_id="corridor_sensor",
                             occurred_at="2026-09-18T23:05:00", event_id="i4")
        e2 = self.home.event("motion_detected", device_id="corridor_sensor",
                             occurred_at="2026-09-18T23:06:00", event_id="i5")
        self.home.handle(e1)
        _, replayed = self.home.handle(e2)
        self.assertFalse(replayed)


class PowerOffDedupTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()
        self.home.rules.add(
            rule_id="ac_kill_rule", name="空调断电",
            trigger="ac_kill_switch", conditions={},
            device_id="bedroom_ac", command="power_off", params={},
            requester_id="dad", safety=SafetyLevel.COMFORT,
            scope=AuthScope.EXPLICIT_CONSENT,
            created_at="2026-09-10T10:00:00",
        )

    def test_kill_switch_duplicate_does_not_power_off_twice(self):
        event = self.home.event(
            "ac_kill_switch", device_id="bedroom_ac",
            occurred_at="2026-09-19T03:00:00", event_id="p1",
            dedup_key="kill-1",
        )
        self.home.handle(event)
        _, replayed = self.home.handle(event)
        self.assertTrue(replayed)
        offs = [a for a in self.home.executor.actions
                if a["command"] == "power_off"]
        self.assertEqual(len(offs), 1)


if __name__ == "__main__":
    unittest.main()
