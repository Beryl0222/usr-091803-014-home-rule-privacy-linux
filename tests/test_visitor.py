"""访客临时通行：有效期、范围、撤销、断网仍有效、使用留痕。"""

import unittest

from domain import Outcome
from household import Household
from tests.test_arbitration import by_rule


class VisitorPassTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def request(self, eid, at, channel="local"):
        return self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="front_door_lock", channel=channel,
            occurred_at=at, event_id=eid,
        ))

    def test_valid_pass_unlocks_in_window(self):
        decision, _ = self.request("v1", "2026-09-19T10:00:00")
        self.assertEqual(decision.winner_keys, ["visitor_daytime_admit:1"])

    def test_expired_pass_denied(self):
        decision, _ = self.request("v2", "2026-09-19T19:00:00")
        self.assertIn("visitor_pass_expired",
                      by_rule(decision)[("visitor_daytime_admit", 1)].reasons)

    def test_not_yet_valid_pass_denied(self):
        decision, _ = self.request("v3", "2026-09-19T07:30:00")
        # 凭证 08:00 生效；同时规则时间窗也是 08:00，至少被其中之一拒绝
        row = by_rule(decision)[("visitor_daytime_admit", 1)]
        self.assertEqual(row.outcome, Outcome.DENIED)

    def test_revoked_pass_denied(self):
        self.home.access.revoke("pass-cleaner")
        decision, _ = self.request("v4", "2026-09-19T10:00:00")
        self.assertIn("visitor_pass_revoked",
                      by_rule(decision)[("visitor_daytime_admit", 1)].reasons)

    def test_pass_scope_limited_to_lock_unlock(self):
        # 凭证不能用于其他设备；构造引用同凭证的越权事件
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="bedroom_ac", occurred_at="2026-09-19T10:00:00",
            event_id="v5",
        ))
        # 没有匹配 bedroom_ac 的规则，候选为空：凭证范围被 access 层强制
        reason = self.home.access.check(
            "pass-cleaner", "bedroom_ac", "unlock", at="2026-09-19T10:00:00"
        )
        self.assertEqual(reason, "visitor_pass_scope_mismatch")
        self.assertEqual(decision.winner_keys, [])

    def test_pass_works_offline(self):
        self.home.set_cloud(False)
        decision, _ = self.request("v6", "2026-09-19T11:00:00")
        self.assertEqual(decision.winner_keys, ["visitor_daytime_admit:1"])
        self.assertEqual(decision.winner.mode, "local")

    def test_successful_use_is_counted(self):
        self.request("v7", "2026-09-19T10:00:00")
        visitor_pass = next(p for p in self.home.access.all()
                            if p.id == "pass-cleaner")
        self.assertEqual(visitor_pass.use_count, 1)

    def test_failed_use_not_counted(self):
        self.request("v8", "2026-09-19T20:00:00")
        visitor_pass = next(p for p in self.home.access.all()
                            if p.id == "pass-cleaner")
        self.assertEqual(visitor_pass.use_count, 0)

    def test_unknown_pass_denied_and_no_unlock(self):
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-stranger",
            device_id="front_door_lock",
            occurred_at="2026-09-19T10:00:00", event_id="v9",
        ))
        rows = by_rule(decision)
        # 访客规则因凭证不存在被拒
        self.assertIn("visitor_pass_scope_mismatch",
                      rows[("visitor_daytime_admit", 1)].reasons)
        # 唯一可能执行的只是云端缺省锁门，绝不放行开门
        for winner in decision.winners:
            self.assertNotEqual(winner.command, "unlock")
        unlocks = [a for a in self.home.executor.actions
                   if a["command"] == "unlock"]
        self.assertEqual(unlocks, [])


if __name__ == "__main__":
    unittest.main()
