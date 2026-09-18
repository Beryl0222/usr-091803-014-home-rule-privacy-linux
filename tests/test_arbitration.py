"""仲裁器：安全级别 → 授权范围 → 时间的唯一决定与可解释理由。"""

import unittest

from home.arbitration import (
    AUTHORITY_ADMIN,
    AUTHORITY_MEMBER,
    AUTHORITY_VISITOR,
    Arbitrator,
    Candidate,
)
from home.models import DeviceCapability, RuleAction


def cand(source, cmd, level=0, authority=AUTHORITY_MEMBER, ts=100.0,
         device="d", name=None, params=None):
    return Candidate(
        action=RuleAction(device, DeviceCapability.LOCK, cmd, params or {}),
        source_id=source, source_name=name or source, actor_id=source,
        safety_level=level, authority=authority, event_ts=ts,
    )


class ArbitrationTest(unittest.TestCase):
    def setUp(self):
        self.a = Arbitrator()

    def test_single_action_passes(self):
        result = self.a.arbitrate([cand("r1", "unlock")])
        self.assertEqual(len(result.decisions), 1)
        self.assertFalse(result.decisions[0].conflicted)

    def test_safety_level_wins_first(self):
        high = cand("safety", "lock", level=5, authority=AUTHORITY_VISITOR)
        low = cand("convenience", "unlock", level=1, authority=AUTHORITY_ADMIN, ts=999.0)
        result = self.a.arbitrate([low, high])
        d = result.decisions[0]
        self.assertTrue(d.conflicted)
        self.assertEqual(d.winner.action.command, "lock")
        self.assertIn("安全级别", d.suppressed[0].reason)

    def test_authority_breaks_tie_when_level_equal(self):
        visitor = cand("visitor", "unlock", level=2, authority=AUTHORITY_VISITOR)
        admin = cand("admin", "lock", level=2, authority=AUTHORITY_ADMIN)
        result = self.a.arbitrate([visitor, admin])
        self.assertEqual(result.decisions[0].winner.action.command, "lock")
        self.assertIn("授权范围", result.decisions[0].suppressed[0].reason)

    def test_latest_time_wins_when_level_and_authority_equal(self):
        older = cand("r1", "off", level=1, authority=AUTHORITY_MEMBER, ts=100.0)
        newer = cand("r2", "on", level=1, authority=AUTHORITY_MEMBER, ts=200.0)
        result = self.a.arbitrate([older, newer])
        self.assertEqual(result.decisions[0].winner.action.command, "on")
        self.assertIn("更新的意图", result.decisions[0].suppressed[0].reason)

    def test_deterministic_tiebreak(self):
        c1 = cand("r1", "off", level=1, authority=AUTHORITY_MEMBER, ts=100.0)
        c2 = cand("r2", "on", level=1, authority=AUTHORITY_MEMBER, ts=100.0)
        first = self.a.arbitrate([c1, c2]).decisions[0].winner.source_id
        second = self.a.arbitrate([c2, c1]).decisions[0].winner.source_id
        self.assertEqual(first, second)

    def test_different_devices_do_not_compete(self):
        a = cand("r1", "on", device="d1")
        b = cand("r2", "off", device="d2")
        result = self.a.arbitrate([a, b])
        self.assertEqual(len(result.decisions), 2)
        self.assertTrue(all(not d.conflicted for d in result.decisions))

    def test_same_command_not_conflicting(self):
        a = cand("r1", "lock", device="d1")
        b = cand("r2", "lock", device="d1")
        d = self.a.arbitrate([a, b]).decisions[0]
        self.assertEqual(d.winner.action.command, "lock")
        self.assertEqual(len(d.suppressed), 1)
        self.assertIn("无需重复", d.suppressed[0].reason)

    def test_explanation_names_winner_and_loser(self):
        high = cand("safety", "lock", level=5, name="夜间安全上锁")
        low = cand("visitor", "unlock", level=0, name="访客开锁")
        d = self.a.arbitrate([high, low]).decisions[0]
        text = d.explain()
        self.assertIn("夜间安全上锁", text)
        self.assertIn("访客开锁", text)


if __name__ == "__main__":
    unittest.main()
