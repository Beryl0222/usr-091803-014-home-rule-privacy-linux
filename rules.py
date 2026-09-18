"""规则版本管理。

每次修改（内容变更/禁用）都产生新版本，旧版本置为 SUPERSEDED 永久保留，
执行记录引用 rule_id + version，因此"某个动作为什么发生"永远可按当时版本复盘。
仅在当前激活版本上匹配事件。
"""

from __future__ import annotations

from typing import Any, Optional

from domain import AuthScope, Rule, RuleStatus, SafetyLevel
from clock_utils import now_iso


class RuleStore:
    def __init__(self):
        # rule_id -> 按版本号排列的全部历史版本
        self._rules: dict[str, list[Rule]] = {}

    def add(self, rule_id: str, name: str, trigger: str,
            conditions: dict[str, Any], device_id: str, command: str,
            params: dict[str, Any], requester_id: str,
            safety: SafetyLevel, scope: AuthScope,
            origin: str = "local", purpose: Optional[str] = None,
            data_use: Optional[dict[str, str]] = None,
            source_device: Optional[str] = None,
            created_at: Optional[str] = None) -> Rule:
        """新建规则，版本从 1 开始。"""
        if rule_id in self._rules:
            raise ValueError(f"规则已存在，修改请用 update: {rule_id}")
        rule = Rule(
            rule_id=rule_id, version=1, name=name, trigger=trigger,
            source_device=source_device,
            conditions=conditions, device_id=device_id, command=command,
            params=params or {}, requester_id=requester_id, safety=safety,
            scope=scope, origin=origin, purpose=purpose, data_use=data_use,
            created_at=created_at or now_iso(),
        )
        self._rules[rule_id] = [rule]
        return rule

    def update(self, rule_id: str, **changes) -> Rule:
        """基于当前激活版本创建新版本；内容无变化不产生新版本。"""
        history = self._rules.get(rule_id)
        if not history:
            raise KeyError(f"规则不存在: {rule_id}")
        current = self.active(rule_id)
        if current is None:
            raise ValueError(f"规则已禁用，无法修改: {rule_id}")
        mutable = {"name", "trigger", "source_device", "conditions",
                   "device_id", "command", "params", "requester_id",
                   "safety", "scope", "origin", "purpose", "data_use"}
        new_kwargs = {
            "name": current.name, "trigger": current.trigger,
            "source_device": current.source_device,
            "conditions": current.conditions, "device_id": current.device_id,
            "command": current.command, "params": dict(current.params),
            "requester_id": current.requester_id, "safety": current.safety,
            "scope": current.scope, "origin": current.origin,
            "purpose": current.purpose, "data_use": current.data_use,
        }
        for key, value in changes.items():
            if key not in mutable:
                raise ValueError(f"不可修改的字段: {key}")
            new_kwargs[key] = value
        if all(getattr(current, k) == v for k, v in new_kwargs.items()):
            return current  # 无实质变化，幂等
        current.status = RuleStatus.SUPERSEDED
        new_rule = Rule(
            rule_id=rule_id, version=current.version + 1,
            parent_version=current.version, status=RuleStatus.ACTIVE,
            created_at=now_iso(),
            **new_kwargs,
        )
        history.append(new_rule)
        return new_rule

    def disable(self, rule_id: str) -> Rule:
        """禁用同样留痕：新版本状态为 DISABLED，引擎不再匹配。"""
        current = self.active(rule_id)
        if current is None:
            raise ValueError(f"规则无激活版本: {rule_id}")
        current.status = RuleStatus.SUPERSEDED
        history = self._rules[rule_id]
        disabled = Rule(
            rule_id=rule_id, version=current.version + 1,
            parent_version=current.version, name=current.name,
            trigger=current.trigger, source_device=current.source_device,
            conditions=current.conditions,
            device_id=current.device_id, command=current.command,
            params=dict(current.params), requester_id=current.requester_id,
            safety=current.safety, scope=current.scope, origin=current.origin,
            purpose=current.purpose, data_use=current.data_use,
            status=RuleStatus.DISABLED,
            created_at=now_iso(),
        )
        history.append(disabled)
        return disabled

    def active(self, rule_id: str) -> Optional[Rule]:
        for rule in reversed(self._rules.get(rule_id, [])):
            if rule.status == RuleStatus.ACTIVE:
                return rule
        return None

    def matching(self, event_type: str,
                 source_device: Optional[str] = None) -> list[Rule]:
        result = []
        for rule_id in self._rules:
            rule = self.active(rule_id)
            if rule is None or rule.trigger != event_type:
                continue
            if rule.source_device is not None and rule.source_device != source_device:
                continue
            result.append(rule)
        return result

    def get_version(self, rule_id: str, version: int) -> Optional[Rule]:
        for rule in self._rules.get(rule_id, []):
            if rule.version == version:
                return rule
        return None

    def history(self, rule_id: str) -> list[Rule]:
        return list(self._rules.get(rule_id, []))

    def history_ids(self) -> list[str]:
        return list(self._rules.keys())

    def to_data(self) -> list[dict]:
        return [r.to_dict() for history in self._rules.values() for r in history]

    @classmethod
    def from_data(cls, rows: list[dict]) -> "RuleStore":
        store = cls()
        for row in sorted(rows or [], key=lambda r: (r["rule_id"], r["version"])):
            rule = Rule.from_dict(row)
            store._rules.setdefault(rule.rule_id, []).append(rule)
        return store
