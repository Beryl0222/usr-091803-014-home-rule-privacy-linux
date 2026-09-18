"""编排引擎：候选生成 → 条件/授权/同意/准入 → 冲突仲裁 → 执行 → 审计。

仲裁保证唯一、可解释、可复现：
- 同一台设备上彼此竞争的候选，按 (安全级别, 授权范围, 规则确立时间) 全序排序，
  级别最高、授权最具体、约定最早者唯一获胜，其余记 SUPPRESSED；
- 不同设备互不压制，可并行执行；
- 决定与每个落选/拒绝原因一并入审计。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from conditions import FALSE as COND_FALSE
from conditions import UNKNOWN as COND_UNKNOWN
from conditions import evaluate
from domain import (
    AuthScope,
    CandidateResult,
    DataType,
    Decision,
    Event,
    Outcome,
    REASON_CAPABILITY_UNKNOWN,
    REASON_CLOUD_OFFLINE,
    REASON_CONDITION_UNKNOWN,
    REASON_CONDITION_UNMET,
    REASON_CONSENT_MISSING,
    REASON_DEVICE_DEGRADED,
    REASON_DEVICE_OFFLINE,
    REASON_LOST_CONFLICT,
    REASON_SCOPE_DENIED,
    Rule,
    SafetyLevel,
)
from clock_utils import now_iso


class ActionExecutor:
    """实际下发通道的接口；生产环境对接本地驱动。"""

    def execute(self, *, device_id: str, command: str, params: dict[str, Any],
                mode: str, event: Event) -> None:
        raise NotImplementedError


class RecordingExecutor(ActionExecutor):
    """测试/联调用执行器：记录每一次真正下发的动作。"""

    def __init__(self):
        self.actions: list[dict[str, Any]] = []

    def execute(self, *, device_id, command, params, mode, event):
        self.actions.append({
            "device_id": device_id,
            "command": command,
            "params": params,
            "mode": mode,
            "event_id": event.id,
            "at": event.occurred_at,
        })


class Orchestrator:
    def __init__(self, members: dict[str, Any], devices, rules, consents,
                 access, executor: ActionExecutor,
                 processed: Optional[dict[str, str]] = None,
                 id_prefix: str = "dec"):
        self.members = members
        self.devices = devices
        self.rules = rules
        self.consents = consents
        self.access = access
        self.executor = executor
        # 去重指纹 -> 已产生的 decision_id
        self.processed: dict[str, str] = processed if processed is not None else {}
        self._id_prefix = id_prefix
        self._counter = len(self.processed)

    # ---- 去重指纹 ----
    def fingerprint(self, event: Event) -> str:
        # 优先级：显式去重键 > 上报方赋予的稳定事件 ID > 事件内容签名。
        # 内容签名兜底无 ID 的网关事件：同设备/类型/时刻/读数视为同一物理事件。
        if event.dedup_key:
            return f"key:{event.dedup_key}"
        if event.id:
            return f"id:{event.id}"
        material = json.dumps(
            [event.type, event.device_id, event.actor_id,
             event.channel, event.occurred_at, event.payload],
            ensure_ascii=False, sort_keys=True,
        )
        return "sig:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    # ---- 事件处理 ----
    def process(self, event: Event, decisions: list[Decision],
                cloud_connected: bool = True) -> tuple[Decision, bool]:
        """处理一次事件。decisions 为历史决定（用于重复回放）。

        返回 (决定, 是否重复回放)；重复事件回放原决定且不再次执行。
        """
        fp = self.fingerprint(event)
        previous_id = self.processed.get(fp)
        if previous_id is not None:
            previous = next((d for d in decisions if d.id == previous_id), None)
            if previous is not None:
                return previous, True

        results: list[CandidateResult] = []
        for rule in self.rules.matching(event.type, event.device_id):
            results.append(self._evaluate_rule(rule, event, cloud_connected))
        # 手动直接指令也作为候选参与同一轮仲裁
        if event.payload.get("manual_command"):
            results.append(self._evaluate_manual(event, cloud_connected))

        admissible = [r for r in results if r.outcome == Outcome.EXECUTED]
        winner_keys = self._arbitrate(admissible)
        winner_set = set(winner_keys)

        for result in results:
            key = f"{result.rule_id}:{result.rule_version}"
            if result in admissible and key not in winner_set:
                result.outcome = Outcome.SUPPRESSED
                result.reasons = (REASON_LOST_CONFLICT, *result.reasons)
            if key in winner_set:
                result.mode = self._execution_mode(result, event, cloud_connected)
                self.executor.execute(
                    device_id=result.device_id,
                    command=result.command,
                    params=self._params_for(result, event),
                    mode=result.mode,
                    event=event,
                )
        # 排序：获胜在前，名次稳定
        results.sort(key=lambda r: (
            r.rank is None, r.rank if r.rank is not None else 99,
            r.rule_id, r.rule_version,
        ))

        self._counter += 1
        decision = Decision(
            id=f"{self._id_prefix}-{self._counter}",
            event_id=event.id,
            event_type=event.type,
            dedup_fingerprint=fp,
            decided_at=event.occurred_at or now_iso(),
            cloud_connected=cloud_connected,
            results=results,
            winner_keys=winner_keys,
        )
        self.processed[fp] = decision.id
        return decision, False

    # ---- 单条规则评估 ----
    def _evaluate_rule(self, rule: Rule, event: Event,
                       cloud_connected: bool) -> CandidateResult:
        result = CandidateResult(
            rule_id=rule.rule_id, rule_version=rule.version,
            device_id=rule.device_id, command=rule.command,
            requester_id=rule.requester_id, safety=rule.safety,
            scope=rule.scope, outcome=Outcome.EXECUTED, purpose=rule.purpose,
        )

        def deny(reason: str) -> CandidateResult:
            result.outcome = Outcome.DENIED
            result.reasons = (reason,)
            return result

        # 1) 条件三态
        verdict = evaluate(rule, event, self.devices.devices, self.members)
        if verdict == COND_FALSE:
            return deny(REASON_CONDITION_UNMET)
        if verdict == COND_UNKNOWN:
            if rule.safety != SafetyLevel.EMERGENCY:
                return deny(REASON_CONDITION_UNKNOWN)
            result.notes = (
                "emergency_fail_safe:sensor_missing",
                *result.notes,
            )
        if rule.data_use:
            result.sensitive = True

        # 2) 设备/能力存在性
        device = self.devices.devices.get(rule.device_id)
        if device is None:
            return deny(REASON_DEVICE_OFFLINE)
        capability = device.capabilities.get(rule.command)
        if capability is None:
            return deny(REASON_CAPABILITY_UNKNOWN)
        result.sensitive = capability.data_type is not None

        # 3) 授权范围
        scope_reason = self._check_scope(rule, event, cloud_connected)
        if scope_reason:
            return deny(scope_reason)

        # 4) 敏感数据同意（动作读数据 + 规则条件额外使用的数据，两者都需获准用途）
        consent_reason = self._check_consent(
            rule, event, capability.data_type.value
            if capability.data_type else None
        )
        if consent_reason:
            return deny(consent_reason)
        if rule.data_use:
            subject_id = (event.payload.get("subject_id")
                          or event.actor_id or rule.requester_id)
            allowed, data_reason = self.consents.check(
                subject_id, DataType(rule.data_use["data_type"]),
                rule.data_use["purpose"], at=event.occurred_at,
            )
            if not allowed:
                return deny(data_reason)

        # 5) 设备状态准入（升级/降级/离线）
        admit_reason = self.devices.admit(
            device, capability, cloud_connected, event.channel
        )
        if admit_reason:
            # 紧急动作故障安全：传感器降级时仍放行并留痕；升级/离线不豁免。
            if (rule.safety == SafetyLevel.EMERGENCY
                    and admit_reason == REASON_DEVICE_DEGRADED):
                result.notes = (
                    "emergency_fail_safe:degraded_capability",
                    *result.notes,
                )
            else:
                return deny(admit_reason)
        return result

    def _evaluate_manual(self, event: Event,
                         cloud_connected: bool) -> CandidateResult:
        command = event.payload["manual_command"]
        params = event.payload.get("params", {})
        scope = AuthScope(event.payload.get("scope", AuthScope.EXPLICIT_CONSENT.value))
        device = self.devices.devices.get(event.device_id or "")
        safety = SafetyLevel.CONVENIENCE
        if device is not None:
            capability = device.capabilities.get(command)
            if capability is not None:
                safety = capability.safety
        rule = Rule(
            rule_id=f"manual:{event.actor_id}", version=0,
            name="手动指令", trigger=event.type, conditions={},
            device_id=event.device_id or "", command=command, params=params,
            requester_id=event.actor_id or "anonymous", safety=safety,
            scope=scope, origin="local",
            purpose=event.payload.get("purpose"),
            created_at=event.occurred_at,
        )
        return self._evaluate_rule(rule, event, cloud_connected)

    # ---- 授权 ----
    def _check_scope(self, rule: Rule, event: Event,
                     cloud_connected: bool) -> Optional[str]:
        # 规则请求人必须是本户成员（manual 候选的 requester 已在成员表内）
        if rule.requester_id not in self.members:
            return REASON_SCOPE_DENIED
        if rule.scope == AuthScope.CLOUD_DEFAULT and not cloud_connected:
            return REASON_CLOUD_OFFLINE
        if rule.scope == AuthScope.VISITOR:
            if not event.actor_id:
                return REASON_SCOPE_DENIED
            return self.access.check(
                event.actor_id, rule.device_id, rule.command,
                at=event.occurred_at,
            )
        if rule.scope in (AuthScope.EXPLICIT_CONSENT, AuthScope.ROLE):
            # 系统事件（设备自身上报）以规则请求人作为授权来源，允许；
            # 带行为人的事件，行为人必须是本户成员。
            if event.actor_id is not None and event.actor_id not in self.members:
                return REASON_SCOPE_DENIED
        return None

    # ---- 同意 ----
    def _check_consent(self, rule: Rule, event: Event,
                       data_type: Optional[str]) -> Optional[str]:
        if data_type is None:
            return None
        if not rule.purpose:
            return REASON_CONSENT_MISSING
        subject_id = (
            event.payload.get("subject_id")
            or event.actor_id
            or rule.requester_id
        )
        allowed, reason = self.consents.check(
            subject_id, DataType(data_type), rule.purpose,
            at=event.occurred_at,
        )
        return reason if not allowed else None

    # ---- 仲裁 ----
    def _arbitrate(self, admissible: list[CandidateResult]) -> list[str]:
        """每台设备一个唯一获胜者；同时写入名次 rank（0 起）。"""
        by_device: dict[str, list[CandidateResult]] = {}
        for result in admissible:
            by_device.setdefault(result.device_id, []).append(result)
        winners: list[str] = []
        for device_id, group in by_device.items():
            ordered = sorted(
                group,
                key=lambda r: (
                    r.safety.rank,
                    r.scope.rank,
                    self._rule_created(r),
                    r.rule_id,
                    r.rule_version,
                ),
            )
            for index, result in enumerate(ordered):
                result.rank = index
            top = ordered[0]
            winners.append(f"{top.rule_id}:{top.rule_version}")
        return winners

    def _rule_created(self, result: CandidateResult) -> str:
        rule = self.rules.get_version(result.rule_id, result.rule_version)
        return rule.created_at if rule else result.rule_id

    def _execution_mode(self, result: CandidateResult, event: Event,
                        cloud_connected: bool) -> str:
        rule = self.rules.get_version(result.rule_id, result.rule_version)
        origin = rule.origin if rule else "local"
        if origin == "cloud" and cloud_connected and event.channel != "local":
            return "cloud"
        return "local"

    def _params_for(self, result: CandidateResult, event: Event) -> dict[str, Any]:
        rule = self.rules.get_version(result.rule_id, result.rule_version)
        if rule is not None:
            return dict(rule.params)
        return dict(event.payload.get("params", {}))
