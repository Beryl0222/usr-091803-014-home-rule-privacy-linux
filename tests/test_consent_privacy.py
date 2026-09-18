"""同意与用途限制：摄像/健康只能为获准用途读取；撤回后停止新的使用。"""

import unittest

from domain import DataType, Outcome
from household import Household
from tests.test_arbitration import by_rule


class ConsentTest(unittest.TestCase):
    def setUp(self):
        self.home = Household.seeded()

    def camera_event(self, eid, at="2026-09-18T10:00:00"):
        return self.home.event(
            "camera_view_requested", actor_id="dad",
            device_id="living_camera", occurred_at=at, event_id=eid,
            payload={"subject_id": "household"},
        )

    def test_permitted_purpose_executes(self):
        decision, _ = self.home.handle(self.camera_event("c1"))
        self.assertIn("camera_security_day:1", decision.winner_keys)
        self.assertTrue(decision.winner.sensitive)
        self.assertEqual(decision.winner.purpose, "home_security")

    def test_unpermitted_purpose_is_denied(self):
        # 云端 AI 抽帧用途从未获准
        decision, _ = self.home.handle(self.camera_event("c2"))
        row = by_rule(decision)[("cloud_ai_snapshot", 1)]
        self.assertEqual(row.outcome, Outcome.DENIED)
        self.assertEqual(row.reasons, ("consent_missing",))

    def test_other_purpose_still_denied_after_security_consent(self):
        # 即便有安防同意，云 AI 训练用途仍不可读
        decision, _ = self.home.handle(self.camera_event("c3"))
        row = by_rule(decision)[("cloud_ai_snapshot", 1)]
        self.assertIn("consent_missing", row.reasons)

    def test_withdraw_stops_new_use(self):
        self.home.consents.withdraw("consent-camera-security",
                                    withdrawn_at="2026-09-18T12:00:00")
        decision, _ = self.home.handle(self.camera_event(
            "c4", at="2026-09-18T13:00:00"))
        row = by_rule(decision)[("camera_security_day", 1)]
        self.assertEqual(row.outcome, Outcome.DENIED)
        self.assertEqual(row.reasons, ("consent_withdrawn",))

    def test_withdraw_does_not_erase_prior_decisions(self):
        decision_before, _ = self.home.handle(self.camera_event(
            "c5", at="2026-09-18T09:00:00"))
        self.home.consents.withdraw("consent-camera-security",
                                    withdrawn_at="2026-09-18T12:00:00")
        # 历史执行记录仍可按可见性复盘
        projection = self.home.audit.explain(
            decision_before.id, "dad", self.home.rules, self.home.devices
        )
        self.assertIsNotNone(projection)
        self.assertTrue(
            any(r.get("command") == "stream" for r in projection["results"])
        )

    def test_health_data_needs_guardian_consent_for_child(self):
        # 孩子手环数据：已授权 sleep_adjust
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-18T23:30:00", event_id="c6",
            payload={"subject_id": "kid",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertIn("sleep_thermal_adjust:1", decision.winner_keys)

    def test_withdraw_child_health_consent_stops_automation(self):
        self.home.consents.withdraw("consent-kid-sleep",
                                    withdrawn_at="2026-09-18T23:40:00")
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-19T00:30:00", event_id="c7",
            payload={"subject_id": "kid",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertIn("consent_withdrawn",
                      by_rule(decision)[("sleep_thermal_adjust", 1)].reasons)

    def test_withdraw_for_one_subject_leaves_others_intact(self):
        self.home.consents.withdraw("consent-kid-sleep",
                                    withdrawn_at="2026-09-18T23:40:00")
        decision, _ = self.home.handle(self.home.event(
            "sleep_reading", device_id="sleep_band",
            occurred_at="2026-09-19T00:31:00", event_id="c8",
            payload={"subject_id": "grandpa",
                     "readings": {"sleep_band": {"breathing": 10}}},
        ))
        self.assertIn("sleep_thermal_adjust:1", decision.winner_keys)

    def test_sensitive_rule_without_purpose_declared_denied(self):
        # 直接构造一个读视频但未声明用途的规则
        from domain import AuthScope, SafetyLevel
        self.home.rules.add(
            rule_id="camera_no_purpose", name="未声明用途的摄像规则",
            trigger="camera_view_requested", conditions={},
            device_id="living_camera", command="stream", params={},
            requester_id="dad", safety=SafetyLevel.SAFETY,
            scope=AuthScope.EXPLICIT_CONSENT, purpose=None,
            created_at="2026-09-10T10:00:00",
        )
        decision, _ = self.home.handle(self.camera_event("c9"))
        self.assertEqual(
            by_rule(decision)[("camera_no_purpose", 1)].reasons,
            ("consent_missing",),
        )

    def test_consent_registry_distinguishes_missing_and_withdrawn(self):
        allowed, reason = self.home.consents.check(
            "kid", DataType.HEALTH, "sleep_adjust", at="2026-09-18T23:00:00"
        )
        self.assertTrue(allowed)
        self.home.consents.withdraw("consent-kid-sleep",
                                    withdrawn_at="2026-09-18T23:40:00")
        allowed, reason = self.home.consents.check(
            "kid", DataType.HEALTH, "sleep_adjust", at="2026-09-19T00:00:00"
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "consent_withdrawn")
        allowed, reason = self.home.consents.check(
            "kid", DataType.HEALTH, "cloud_ai_training"
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "consent_missing")


if __name__ == "__main__":
    unittest.main()
