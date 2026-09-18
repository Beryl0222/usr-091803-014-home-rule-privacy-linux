"""本地保底：断网时云端规则失效，但门锁等 local_always 能力仍受本地控制。"""

import unittest

from household import Household
from tests.test_arbitration import by_rule


class LocalResilienceTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def test_cloud_rule_denied_offline_but_visitor_unlocks_locally(self):
        self.home.set_cloud(False)
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="front_door_lock",
            occurred_at="2026-09-19T11:00:00", event_id="o1",
        ))
        rows = by_rule(decision)
        self.assertEqual(decision.winner_keys, ["visitor_daytime_admit:1"])
        self.assertEqual(
            rows[("cloud_door_default", 1)].reasons,
            ("cloud_rule_unavailable_offline",),
        )
        self.assertEqual(decision.winner.mode, "local")
        self.assertEqual(
            self.home.executor.actions[-1]["command"], "unlock"
        )

    def test_offline_lock_still_works_over_local_channel(self):
        self.home.devices.heartbeat_lost("front_door_lock")
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="front_door_lock", channel="local",
            occurred_at="2026-09-19T12:00:00", event_id="o2",
        ))
        self.assertEqual(decision.winner_keys, ["visitor_daytime_admit:1"])

    def test_offline_lock_rejects_remote_channel(self):
        self.home.devices.heartbeat_lost("front_door_lock")
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="front_door_lock", channel="remote",
            occurred_at="2026-09-19T12:05:00", event_id="o3",
        ))
        rows = by_rule(decision)
        self.assertIn("device_offline",
                      rows[("visitor_daytime_admit", 1)].reasons)
        self.assertEqual(decision.winner_keys, [])

    def test_non_local_always_device_offline_blocks_all(self):
        self.home.devices.heartbeat_lost("bedroom_ac")
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-19T23:30:00", event_id="o4",
            payload={"subject_id": "kid",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertIn("device_offline",
                      by_rule(decision)[("sleep_thermal_adjust", 1)].reasons)

    def test_reconnect_requires_reverification_before_full_service(self):
        self.home.devices.heartbeat_lost("bedroom_ac")
        self.home.devices.heartbeat("bedroom_ac", at="2026-09-19T08:00:00")
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-19T08:01:00", event_id="o5",
            payload={"subject_id": "kid",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertIn("device_recovering_unverified",
                      by_rule(decision)[("sleep_thermal_adjust", 1)].reasons)
        self.home.devices.verify_capabilities(
            "bedroom_ac", ("temperature",), at="2026-09-19T08:02:00"
        )
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-19T08:03:00", event_id="o6",
            payload={"subject_id": "kid",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertEqual(decision.winner_keys, ["sleep_thermal_adjust:1"])


if __name__ == "__main__":
    unittest.main()
