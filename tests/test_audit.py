"""审计可见性与推断防护。

- 成员只能查询自己有权知道的决定，无权与不存在不可区分（None/404）；
- 有权查看时，只看到自己候选的完整解释，他人候选仅保留仲裁要素；
- 紧急安全事件全户可见，但非请求人仍看不到无关设备细节。
"""

import unittest

from household import Household


class AuditVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def test_requester_sees_full_own_explanation(self):
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="a1",
        ))
        projection = self.home.audit.explain(
            decision.id, "grandpa", self.home.rules, self.home.devices
        )
        own = [r for r in projection["results"]
               if r.get("rule_id") == "elder_night_light"]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["device_id"], "corridor_light")
        self.assertEqual(own[0]["command"], "on")
        self.assertEqual(own[0]["device_name"], "走廊夜灯")
        self.assertEqual(projection["winners"][0]["mode"], "local")

    def test_competitor_sees_own_detail_but_other_is_redacted(self):
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="a2",
        ))
        projection = self.home.audit.explain(
            decision.id, "mom", self.home.rules, self.home.devices
        )
        own = [r for r in projection["results"]
               if r.get("rule_id") == "sleep_house_dim"]
        self.assertEqual(own[0]["command"], "off")
        other = [r for r in projection["results"]
                 if r.get("rule_id") != "sleep_house_dim"]
        self.assertTrue(other)
        for row in other:
            self.assertNotIn("device_id", row)
            self.assertNotIn("command", row)
            self.assertNotIn("device_name", row)
            self.assertNotIn("purpose", row)
            self.assertNotIn("notes", row)
        # 获胜者只暴露仲裁要素
        self.assertEqual(projection["winners"],
                         [{"safety": "safety", "scope": "explicit"}])

    def test_unrelated_member_gets_none(self):
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="a3",
        ))
        self.assertIsNone(self.home.audit.explain(
            decision.id, "kid", self.home.rules, self.home.devices))

    def test_nonexistent_decision_indistinguishable_from_forbidden(self):
        self.assertIsNone(self.home.audit.explain(
            "dec-nope", "dad", self.home.rules, self.home.devices))

    def test_list_for_only_returns_visible(self):
        self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="a4",
        ))
        self.home.handle(self.home.event(
            "camera_view_requested", actor_id="dad",
            device_id="living_camera", occurred_at="2026-09-18T10:00:00",
            event_id="a5", payload={"subject_id": "household"},
        ))
        mom_ids = {d.id for d in self.home.audit.list_for("mom")}
        dad_ids = {d.id for d in self.home.audit.list_for("dad")}
        self.assertEqual(len(mom_ids), 1)
        self.assertEqual(len(dad_ids), 1)
        self.assertNotEqual(mom_ids, dad_ids)
        self.assertEqual(self.home.audit.list_for("kid"), [])

    def test_emergency_visible_to_all_household_members(self):
        decision, _ = self.home.handle(self.home.event(
            "gas_leak", device_id="kitchen_gas",
            occurred_at="2026-09-19T04:00:00", event_id="a6",
            payload={"readings": {"kitchen_gas": {"gas_level": 40}}},
        ))
        for member_id in ("grandpa", "grandma", "dad", "mom", "kid"):
            self.assertIsNotNone(self.home.audit.explain(
                decision.id, member_id, self.home.rules, self.home.devices))

    def test_visitor_decision_visible_to_host_only(self):
        decision, _ = self.home.handle(self.home.event(
            "door_access_requested", actor_id="pass-cleaner",
            device_id="front_door_lock",
            occurred_at="2026-09-19T10:00:00", event_id="a7",
        ))
        # 接待主人 dad 可见；其他成员看不到访客活动
        self.assertIsNotNone(self.home.audit.explain(
            decision.id, "dad", self.home.rules, self.home.devices))
        self.assertIsNone(self.home.audit.explain(
            decision.id, "grandma", self.home.rules, self.home.devices))

    def test_denied_reasons_of_others_are_generic(self):
        # dad 的摄像决定同时含被拒的云端规则；mom 无权看该决定；
        # 换一个双方都可见、且存在他人被拒候选的构造：紧急事件全户可见。
        # 给燃气事件添加一条会因同意失败的他人同事件规则（健康读取）。
        from domain import (
            AuthScope, DataType, SafetyLevel,
        )
        self.home.rules.add(
            rule_id="sneaky_health_on_gas", name="借燃气事件读健康",
            trigger="gas_leak", conditions={},
            device_id="sleep_band", command="record_session",
            params={}, requester_id="mom",
            safety=SafetyLevel.CONVENIENCE,
            scope=AuthScope.EXPLICIT_CONSENT,
            purpose="cloud_ai_training",
            data_use={"data_type": "health", "purpose": "cloud_ai_training"},
            created_at="2026-09-10T10:00:00",
        )
        decision, _ = self.home.handle(self.home.event(
            "gas_leak", device_id="kitchen_gas",
            occurred_at="2026-09-19T04:10:00", event_id="a8",
            payload={"readings": {"kitchen_gas": {"gas_level": 40}}},
        ))
        projection = self.home.audit.explain(
            decision.id, "grandpa", self.home.rules, self.home.devices
        )
        # 他人被拒候选的原因必须泛化，不得出现 consent 字样
        denied_rows = [
            r for r in projection["results"]
            if r.get("outcome") == "denied"
        ]
        self.assertTrue(denied_rows)
        for row in denied_rows:
            self.assertNotIn("consent", str(row.get("reasons", [])))
            self.assertIn("rule_not_admitted", row.get("reasons", []))
        # 他人候选一律不出现设备/命令/用途
        for row in projection["results"]:
            self.assertNotIn("device_id", row)
            self.assertNotIn("command", row)
            self.assertNotIn("purpose", row)

    def test_explanation_says_why_suppressed(self):
        decision, _ = self.home.handle(self.home.event(
            "motion_detected", device_id="corridor_sensor",
            occurred_at="2026-09-18T23:05:00", event_id="a9",
        ))
        projection = self.home.audit.explain(
            decision.id, "mom", self.home.rules, self.home.devices
        )
        own = next(r for r in projection["results"]
                   if r.get("rule_id") == "sleep_house_dim")
        self.assertIn("lost_conflict", own["reasons"])
        self.assertEqual(own["rank"], 1)


if __name__ == "__main__":
    unittest.main()
