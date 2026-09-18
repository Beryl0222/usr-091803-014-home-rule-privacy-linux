"""审计与可见性：可解释"为什么发生"，但无法据此推断他人的敏感活动。

每条执行记录在写入时就计算两级可见名单：

* visible_full     可看完整记录：数据主体本人、动作行为人、（非敏感时）规则主人/管理员；
* visible_redacted 只能看脱敏记录：规则主人/管理员对他人的摄像、健康类记录。

脱敏视图剔除：数据主体、行为人、参数载荷、房间/个性化设备名，理由也改写为
不含任何人称的设备级说明，从而无法把"某时刻发生了什么"关联到具体家人。
查询不到的记录直接不返回（与"从不存在"不可区分），避免存在性泄漏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from home.models import (
    ActionOutcome,
    DeviceCapability,
    PrivacyCategory,
    new_id,
    now_ts,
)
from home.store import LocalStore

SENSITIVE = frozenset({PrivacyCategory.IMAGE.value, PrivacyCategory.HEALTH.value})

# 脱敏视图中隐藏的字段（含可见名单本身——名单会反向暴露数据主体是谁）
_REDACTED_KEYS = (
    "actor_id", "subjects", "params", "rule_name", "device_name", "detail",
    "visible_full", "visible_redacted", "redacted_explanation",
)


@dataclass(frozen=True)
class AuditQuery:
    viewer_id: str
    device_id: Optional[str] = None
    event_id: Optional[str] = None


class AuditService:
    def __init__(self, store: LocalStore, clock=now_ts):
        self._store = store
        self._clock = clock

    def record(
        self,
        *,
        event_id: str,
        event_type: str,
        device_id: str,
        device_name: str,
        capability: DeviceCapability,
        command: Optional[str],
        outcome: ActionOutcome,
        explanation: str,
        category: PrivacyCategory,
        actor_id: Optional[str],
        subjects: list[str],
        visible_full: list[str],
        visible_redacted: list[str],
        rule_id: Optional[str] = None,
        rule_name: Optional[str] = None,
        params: Optional[dict[str, Any]] = None,
        detail: Optional[dict[str, Any]] = None,
        replay_of: Optional[str] = None,
    ) -> str:
        ts = self._clock()
        sensitive = category.value in SENSITIVE
        entry = {
            "entry_id": new_id("aud"),
            "ts": ts,
            "event_id": event_id,
            "event_type": event_type,
            "device_id": device_id,
            "device_name": device_name,
            "capability": capability.value,
            "command": command,
            "params": params or {},
            "outcome": outcome.value,
            "category": category.value,
            "sensitive": sensitive,
            "explanation": explanation,
            "redacted_explanation": self._redact_reason(category, outcome, explanation, sensitive),
            "actor_id": actor_id,
            "subjects": sorted(set(subjects)),
            "rule_id": rule_id,
            "rule_name": rule_name,
            "detail": detail or {},
            "replay_of": replay_of,
            "visible_full": sorted(set(visible_full)),
            "visible_redacted": sorted(set(visible_redacted) - set(visible_full)),
        }
        self._store.append("audit", entry)
        return entry["entry_id"]

    def query(self, query: AuditQuery) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in self._store.list_of("audit"):
            if query.device_id and entry["device_id"] != query.device_id:
                continue
            if query.event_id and entry["event_id"] != query.event_id:
                continue
            if query.viewer_id in entry["visible_full"]:
                result.append({**entry, "view": "full"})
            elif query.viewer_id in entry["visible_redacted"]:
                result.append(self._redact(entry))
        result.sort(key=lambda e: (e["ts"], e["entry_id"]))
        return result

    def explain(self, entry_id: str, viewer_id: str) -> Optional[dict[str, Any]]:
        """确认某动作为什么发生；无权知道时返回 None（不泄漏记录是否存在）。"""
        for entry in self._store.list_of("audit"):
            if entry["entry_id"] != entry_id:
                continue
            if viewer_id in entry["visible_full"]:
                return {**entry, "view": "full"}
            if viewer_id in entry["visible_redacted"]:
                return self._redact(entry)
            return None
        return None

    # ---- 内部 -----------------------------------------------------------

    @staticmethod
    def _redact(entry: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in entry.items() if k not in _REDACTED_KEYS}
        # 隐私设备的个性化名称（如"老人卧室摄像头"）会反向定位到人，统一泛化
        if entry.get("sensitive"):
            view["device_id"] = None
            view["explanation"] = entry["redacted_explanation"]
            view["event_type"] = "sensitive_device_activity"
            view["capability_label"] = {
                PrivacyCategory.IMAGE.value: "摄像设备",
                PrivacyCategory.HEALTH.value: "健康设备",
            }.get(entry["category"], "敏感设备")
        else:
            view["explanation"] = entry["redacted_explanation"]
        view["view"] = "redacted"
        return view

    @staticmethod
    def _redact_reason(category: PrivacyCategory, outcome: ActionOutcome,
                       explanation: str, sensitive: bool) -> str:
        if not sensitive:
            return explanation
        if outcome == ActionOutcome.DENIED:
            return "因同意或隐私策略未满足，相关敏感数据未被读取/使用"
        if outcome == ActionOutcome.DEGRADED:
            return "敏感设备因离线、升级或传感器缺失进入降级，未产生新的读取"
        if outcome == ActionOutcome.SUPPRESSED:
            return "该设备动作在冲突仲裁中被更高优先级的安全/隐私策略覆盖"
        return "敏感设备按已授权的自动化策略运行（已隐去人员与房间信息）"


def viewers_for(
    *,
    category: PrivacyCategory,
    subjects: list[str],
    operator_id: Optional[str],
    rule_owner_id: Optional[str],
    admins: list[str],
) -> tuple[list[str], list[str]]:
    """按敏感度计算完整/脱敏可见名单。

    * 完整：数据主体本人；以及直接动手操作的人（手动指令/访客，他们本就知道自己做了什么）；
    * 敏感数据（他人的摄像/健康）：规则主人与管理员只能看到设备级脱敏结果，
      以免由记录推断其他家人的活动；
    * 非敏感：规则主人与管理员可看完整记录以便运维。
    """
    full = set(subjects)
    if operator_id:
        full.add(operator_id)
    redacted: set[str] = set()
    if category.value in SENSITIVE:
        if rule_owner_id:
            redacted.add(rule_owner_id)
        redacted.update(admins)
    else:
        if rule_owner_id:
            full.add(rule_owner_id)
        full.update(admins)
    return sorted(full), sorted(redacted - full)
