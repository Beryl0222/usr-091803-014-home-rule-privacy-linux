"""执行审计与可见性。

两条底线：
1. 成员只能查询自己"有权知道"的决定：仅保留 规则请求人、事件行为人
   （访客事件则为接待主人）在可见集合内；无权查询表现为"查无此记录"，
   不暴露记录是否存在。
2. 即使有权查看某条决定，也只能看到与自己相关动作的完整解释；
   其他成员竞争动作只暴露安全级别/授权范围/名次这类仲裁要素，
   不暴露设备、命令、用途或任何可定位他人敏感活动的信息。
"""

from __future__ import annotations

from typing import Any, Optional

from domain import (
    Decision,
    Event,
    Outcome,
)
from access import AccessControl


# 非本人候选动作可见的泛化原因码：不含任何人/设备/用途信息
GENERIC_DENIED_REASONS = {
    "rule_not_admitted",   # 对方规则未通过条件/授权/设备准入
}


def compute_visibility(event: Event, results: list,
                       access: AccessControl,
                       members: dict[str, Any]) -> set[str]:
    """一条决定的可见成员集合：请求人 + 行为人（访客则为接待主人）。

    若有紧急安全动作实际执行，属于全户安全事件，全体成员均有权知道。
    """
    visible: set[str] = set()
    emergency_executed = False
    for result in results:
        visible.add(result.requester_id)
        if (result.outcome == Outcome.EXECUTED
                and result.safety.value == "emergency"):
            emergency_executed = True
    if emergency_executed:
        visible.update(members.keys())
    if event.actor_id:
        pass_record = next(
            (p for p in access.all() if p.id == event.actor_id), None
        )
        if pass_record is not None:
            visible.add(pass_record.host_id)
        else:
            visible.add(event.actor_id)
    # 数据主体有权知道自己个人数据被哪条自动化使用
    subject_id = event.payload.get("subject_id")
    if subject_id and subject_id in members:
        visible.add(subject_id)
    return visible


def _own_candidate(result, viewer_id: str) -> dict[str, Any]:
    """本人相关候选：完整可解释信息。"""
    data = result.to_dict()
    return data


def _other_candidate(result) -> dict[str, Any]:
    """他人候选：仅保留理解仲裁所需的最小信息。"""
    redacted = {
        "outcome": result.outcome.value,
        "rank": result.rank,
        "safety": result.safety.value,
        "scope": result.scope.value,
    }
    # 拒绝原因泛化，避免暴露他人的同意状态/访客时效/设备细节。
    if result.outcome == Outcome.DENIED:
        redacted["reasons"] = ["rule_not_admitted"]
    elif result.reasons and result.outcome == Outcome.SUPPRESSED:
        redacted["reasons"] = ["lost_conflict"]
    return redacted


class AuditLog:
    def __init__(self, access: AccessControl,
                 members: Optional[dict[str, Any]] = None):
        self.access = access
        self.members = members if members is not None else {}
        self._decisions: list[Decision] = []
        self._visibility: dict[str, set[str]] = {}
        self._by_event: dict[str, str] = {}

    def attach_members(self, members: dict[str, Any]) -> None:
        """成员表晚于审计对象构建/重建时，重新关联。"""
        self.members = members

    @property
    def decisions(self) -> list[Decision]:
        return self._decisions

    def append(self, decision: Decision, event: Event,
               results: list) -> None:
        self._decisions.append(decision)
        self._visibility[decision.id] = compute_visibility(
            event, results, self.access, self.members
        )
        self._by_event[decision.event_id] = decision.id

    def can_see(self, decision_id: str, viewer_id: str) -> bool:
        return viewer_id in self._visibility.get(decision_id, set())

    def get(self, decision_id: str, viewer_id: str) -> Optional[Decision]:
        """无权访问与不存在不可区分（返回 None）。"""
        if not self.can_see(decision_id, viewer_id):
            return None
        return next((d for d in self._decisions if d.id == decision_id), None)

    def list_for(self, viewer_id: str) -> list[Decision]:
        return [
            d for d in self._decisions
            if viewer_id in self._visibility.get(d.id, set())
        ]

    def explain(self, decision_id: str, viewer_id: str,
                rules, devices) -> Optional[dict[str, Any]]:
        """生成面向查询人的解释投影；无权/不存在均返回 None。"""
        decision = self.get(decision_id, viewer_id)
        if decision is None:
            return None
        projected_results: list[dict[str, Any]] = []
        for index, result in enumerate(decision.results):
            if result.requester_id == viewer_id:
                entry = _own_candidate(result, viewer_id)
                entry["rule_name"] = self._rule_name(rules, result)
                entry["device_name"] = self._device_name(devices, result.device_id)
            else:
                entry = _other_candidate(result)
            entry["candidate_index"] = index
            projected_results.append(entry)
        winners = decision.winners
        projection = {
            "decision_id": decision.id,
            "event_id": decision.event_id,
            "event_type": decision.event_type,
            "decided_at": decision.decided_at,
            "cloud_connected": decision.cloud_connected,
            "replayed": decision.replayed,
            "winners": [],
            "results": projected_results,
        }
        for winner in winners:
            if winner.requester_id == viewer_id:
                projection["winners"].append({
                    "rule_id": winner.rule_id,
                    "rule_version": winner.rule_version,
                    "device_id": winner.device_id,
                    "command": winner.command,
                    "safety": winner.safety.value,
                    "scope": winner.scope.value,
                    "mode": winner.mode,
                })
            else:
                # 只解释"为什么不是我的动作执行"所必需的仲裁要素。
                projection["winners"].append({
                    "safety": winner.safety.value,
                    "scope": winner.scope.value,
                })
        return projection

    @staticmethod
    def _rule_name(rules, result) -> Optional[str]:
        rule = rules.get_version(result.rule_id, result.rule_version)
        return rule.name if rule else None

    @staticmethod
    def _device_name(devices, device_id: str) -> Optional[str]:
        device = devices.devices.get(device_id)
        return device.name if device else None

    # ---- 持久化 ----
    def to_data(self) -> dict[str, Any]:
        return {
            "decisions": [d.to_dict() for d in self._decisions],
            "visibility": {
                did: sorted(members) for did, members in self._visibility.items()
            },
            "by_event": self._by_event,
        }

    def load_data(self, data: dict[str, Any]) -> None:
        self._decisions = [Decision.from_dict(d) for d in data.get("decisions", [])]
        self._visibility = {
            did: set(members)
            for did, members in data.get("visibility", {}).items()
        }
        self._by_event = data.get("by_event", {})
