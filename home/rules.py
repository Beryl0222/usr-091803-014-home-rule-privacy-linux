"""规则版本管理：每次修改都留下不可变版本，可审计、可回溯。

当前生效的规则存在 rules 集合；每次创建/修改/启用停用/删除都会向
rule_versions 追加一条不可变快照（含修改者、时间、动作、规则版本号）。
断网时这些版本与规则本身都在本地，仲裁不依赖云端。
"""

from __future__ import annotations

from typing import Any, Optional

from home.models import (
    AutomationRule,
    Device,
    DeviceCapability,
    RuleAction,
    TimeWindow,
    new_id,
    now_ts,
)
from home.store import LocalStore


class RuleConflictError(ValueError):
    """规则本身不合法（引用了不存在的设备/能力、时间窗口非法等）。"""


class RuleBook:
    def __init__(self, store: LocalStore, clock=now_ts):
        self._store = store
        self._clock = clock

    # ---- 查询 -----------------------------------------------------------

    def get(self, rule_id: str) -> Optional[AutomationRule]:
        data = self._store.get("rules", rule_id)
        return AutomationRule.from_dict(data) if data else None

    def list_active(self) -> list[AutomationRule]:
        return [AutomationRule.from_dict(d) for d in self._store.all("rules") if d.get("enabled", True)]

    def list_all(self) -> list[AutomationRule]:
        return [AutomationRule.from_dict(d) for d in self._store.all("rules")]

    def history(self, rule_id: Optional[str] = None) -> list[dict[str, Any]]:
        versions = self._store.list_of("rule_versions")
        if rule_id:
            versions = [v for v in versions if v["rule"]["rule_id"] == rule_id]
        return versions

    # ---- 修改（均产生版本） --------------------------------------------

    def create(
        self,
        name: str,
        owner_id: str,
        trigger: str,
        actions: list[RuleAction],
        devices: dict[str, Device],
        window: Optional[TimeWindow] = None,
        purpose: Optional[str] = None,
        safety_level: int = 0,
    ) -> AutomationRule:
        rule = AutomationRule(
            rule_id=new_id("rule"),
            name=name,
            owner_id=owner_id,
            trigger=trigger,
            actions=tuple(actions),
            window=window,
            purpose=purpose,
            safety_level=safety_level,
            enabled=True,
            version=1,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        self._validate(rule, devices)
        self._store.put("rules", rule.rule_id, rule.to_dict())
        self._record(rule, "create", owner_id)
        return rule

    def modify(
        self,
        rule_id: str,
        editor_id: str,
        devices: dict[str, Device],
        **changes: Any,
    ) -> AutomationRule:
        current = self.get(rule_id)
        if current is None:
            raise KeyError(f"规则不存在: {rule_id}")
        data = current.to_dict()
        allowed = {"name", "trigger", "window", "purpose", "safety_level", "enabled", "actions"}
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"不允许修改字段: {key}")
            if key == "actions":
                data["actions"] = [a.to_dict() if isinstance(a, RuleAction) else a for a in value]
            elif key == "window":
                data["window"] = value.to_dict() if value is not None else None
            else:
                data[key] = value
        data["version"] = current.version + 1
        data["updated_at"] = self._clock()
        updated = AutomationRule.from_dict(data)
        self._validate(updated, devices)
        self._store.put("rules", rule_id, updated.to_dict())
        self._record(updated, "modify", editor_id)
        return updated

    def set_enabled(self, rule_id: str, editor_id: str, enabled: bool) -> AutomationRule:
        current = self.get(rule_id)
        if current is None:
            raise KeyError(f"规则不存在: {rule_id}")
        data = current.to_dict()
        data.update({
            "enabled": enabled,
            "version": current.version + 1,
            "updated_at": self._clock(),
        })
        updated = AutomationRule.from_dict(data)
        self._store.put("rules", rule_id, updated.to_dict())
        self._record(updated, "enable" if enabled else "disable", editor_id)
        return updated

    def delete(self, rule_id: str, editor_id: str) -> None:
        current = self.get(rule_id)
        if current is None:
            return
        # 逻辑删除：从当前集合移除，但保留全部历史版本，并追加一条 delete 记录
        self._store.delete("rules", rule_id)
        self._record(current, "delete", editor_id)

    # ---- 校验 -----------------------------------------------------------

    def _validate(self, rule: AutomationRule, devices: dict[str, Device]) -> None:
        if not rule.actions:
            raise RuleConflictError("规则至少包含一个动作")
        for act in rule.actions:
            device = devices.get(act.device_id)
            if device is None:
                raise RuleConflictError(f"动作引用了不存在的设备: {act.device_id}")
            if act.capability not in device.capabilities:
                raise RuleConflictError(
                    f"设备 {device.name} 不具备能力 {act.capability.value}"
                )
        if rule.window is not None and not (0 <= rule.window.start_hour <= 23
                                            and 0 <= rule.window.end_hour <= 23):
            raise RuleConflictError("时间窗口小时必须在 0..23")
        sensitive = any(
            DeviceCapability(a.capability) in (DeviceCapability.CAMERA_STREAM,)
            for a in rule.actions
        )
        if sensitive and not rule.purpose:
            raise RuleConflictError("涉及摄像画面的规则必须声明用途 purpose")

    # ---- 版本记录 -------------------------------------------------------

    def _record(self, rule: AutomationRule, change: str, editor_id: str) -> None:
        entry = {
            "seq": len(self._store.list_of("rule_versions")) + 1,
            "ts": self._clock(),
            "change": change,
            "editor_id": editor_id,
            "rule": rule.to_dict(),
        }
        self._store.append("rule_versions", entry)
