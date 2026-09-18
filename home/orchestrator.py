"""家庭设备编排入口：串联规则、同意、仲裁、降级恢复、幂等与审计。

一次 process_event 的固定流水线：

    事件去重 → 收集候选（规则/访客/手动）→ 同意与隐私门
        → 冲突仲裁（唯一决定）→ 降级判断/执行 → 写审计（带可见名单）

所有输入状态来自本地 LocalStore 与内存设备运行态，断网照常工作；
云端规则永远不是唯一事实来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from home.arbitration import (
    AUTHORITY_ADMIN,
    AUTHORITY_MEMBER,
    AUTHORITY_VISITOR,
    Arbitrator,
    Candidate,
)
from home.audit import AuditService, viewers_for
from home.models import (
    CAPABILITY_DATA,
    Action,
    ActionOutcome,
    AutomationRule,
    Device,
    DeviceCapability,
    DeviceState,
    DeviceStatus,
    Event,
    Member,
    PrivacyCategory,
    RuleAction,
    VisitorPass,
    new_id,
    now_ts,
)
from home.privacy import ConsentGate, ConsentViolation
from home.resilience import DeviceRegistry, ResiliencePolicy
from home.rules import RuleBook
from home.store import LocalStore

# 这些命令不读取敏感数据，而是停止/关闭采集，属于隐私保护动作，无需同意即可执行
PROTECTIVE_COMMANDS = frozenset({"stop", "off", "disable", "pause"})


@dataclass(frozen=True)
class ExecutedCommand:
    action_id: str
    event_id: str
    device_id: str
    capability: DeviceCapability
    command: str
    params: dict[str, Any] = field(default_factory=dict)
    fallback: bool = False


class HomeOrchestrator:
    def __init__(self, store: LocalStore, clock=now_ts):
        self._store = store
        self._clock = clock
        self.devices = DeviceRegistry()
        self.gate = ConsentGate(store, clock)
        self.rules = RuleBook(store, clock)
        self.audit = AuditService(store, clock)
        self._arbitrator = Arbitrator()
        self._resilience = ResiliencePolicy()
        self.executed: list[ExecutedCommand] = []
        self._executed_keys: set[str] = set()
        self._restore()

    # ---- 配置：成员与设备 ----------------------------------------------

    def register_member(self, member: Member) -> None:
        self._store.put("members", member.member_id, member.to_dict())

    def members(self) -> list[Member]:
        return [Member.from_dict(d) for d in self._store.all("members")]

    def member(self, member_id: str) -> Optional[Member]:
        data = self._store.get("members", member_id)
        return Member.from_dict(data) if data else None

    def register_device(self, device: Device,
                        state: Optional[DeviceState] = None) -> None:
        self._store.put("devices", device.device_id, device.to_dict())
        self.devices.register(device)
        if state is not None:
            if state.status != DeviceStatus.ONLINE:
                self.devices.set_status(device.device_id, state.status)
            if state.available_sensors:
                self.devices.set_sensors(device.device_id, state.available_sensors)

    def device_map(self) -> dict[str, Device]:
        return {d.device_id: d for d in self.devices.all_devices()}

    # ---- 访客凭证 -------------------------------------------------------

    def issue_visitor_pass(
        self, visitor_name: str, valid_from: float, valid_until: float,
        capabilities: Optional[frozenset[DeviceCapability]] = None,
    ) -> VisitorPass:
        visitor_pass = VisitorPass(
            pass_id=new_id("pass"),
            visitor_name=visitor_name,
            capabilities=capabilities or frozenset({DeviceCapability.LOCK}),
            valid_from=valid_from,
            valid_until=valid_until,
        )
        self._store.put("visitor_passes", visitor_pass.pass_id, visitor_pass.to_dict())
        return visitor_pass

    def revoke_visitor_pass(self, pass_id: str) -> None:
        data = self._store.get("visitor_passes", pass_id)
        if data:
            data["revoked"] = True
            self._store.put("visitor_passes", pass_id, data)

    def _visitor_pass(self, pass_id: str) -> Optional[VisitorPass]:
        data = self._store.get("visitor_passes", pass_id)
        return VisitorPass.from_dict(data) if data else None

    # ---- 事件处理 -------------------------------------------------------

    def process_event(self, event: Event) -> dict[str, Any]:
        """处理一个事件；同一 event_id 重复进入不会再次执行任何动作。"""
        prior = self._store.get("processed_events", event.event_id)
        if prior is not None:
            # 重复事件：记录一次去重审计，但绝不再开锁/断电
            self._record_dedup(event, prior)
            return {"deduped": True, "entry_ids": list(prior["entry_ids"])}
        return self._process(event, replay_devices=None)

    def _process(self, event: Event, replay_devices: Optional[set[str]],
                 replay_of: Optional[str] = None) -> dict[str, Any]:
        candidates: list[Candidate] = []
        denied: list[tuple[Candidate, str]] = []
        unauthorized: list[tuple[RuleAction, Optional[str], str]] = []

        self._collect_rule_candidates(event, candidates, denied, unauthorized, replay_devices)
        self._collect_visitor_candidate(event, candidates, denied, replay_devices)
        self._collect_manual_candidate(event, candidates, denied, unauthorized, replay_devices)

        entry_ids: list[str] = []
        # 无授权 / 同意被拒的候选直接留痕（它们不进入仲裁）
        for cand, reason in denied:
            entry_ids.append(self._write_decision_audit(
                event, cand, ActionOutcome.DENIED, reason))
        for action, actor_id, reason in unauthorized:
            entry_ids.append(self._write_plain_audit(
                event, action, ActionOutcome.DENIED, reason, actor_id))

        result = self._arbitrator.arbitrate(candidates)
        pending_devices: set[str] = set()

        for decision in result.decisions:
            if replay_devices is not None and decision.device_id not in replay_devices:
                continue  # 恢复重放时只处理当时被挂起的设备
            winner = decision.winner
            if winner is None:
                continue
            if self._already_executed(event.event_id, decision.device_id,
                                      decision.capability):
                continue
            for suppressed in decision.suppressed:
                entry_ids.append(self._write_decision_audit(
                    event, suppressed.candidate, ActionOutcome.SUPPRESSED,
                    f"冲突仲裁落败：{suppressed.reason}"))

            device = self.devices.get(winner.action.device_id)
            assert device is not None
            degradation = self._resilience.evaluate(
                device, self.devices.state(device.device_id),
                winner.action.command, event.sensors_present,
            )
            if degradation is None:
                entry_ids.append(self._execute(event, winner, device, replay_of))
                self._mark_executed(event.event_id, device.device_id,
                                    decision.capability)
            else:
                ids, pending = self._handle_degradation(
                    event, winner, device, degradation, replay_of)
                entry_ids.extend(ids)
                if pending:
                    pending_devices.add(device.device_id)

        if replay_devices is None:
            self._store.put_map("processed_events", event.event_id, {
                "ts": event.ts,
                "entry_ids": entry_ids,
                "executed_keys": self._executed_keys_of(event.event_id),
            })
        if pending_devices:
            self._enqueue_pending(event, pending_devices)

        return {"deduped": False, "entry_ids": entry_ids,
                "pending_devices": sorted(pending_devices)}

    # ---- 候选收集 -------------------------------------------------------

    def _collect_rule_candidates(
        self, event: Event, candidates: list[Candidate],
        denied: list[tuple[Candidate, str]],
        unauthorized: list[tuple[RuleAction, Optional[str], str]],
        replay_devices: Optional[set[str]],
    ) -> None:
        owner_cache: dict[str, Optional[Member]] = {}
        for rule in self.rules.list_active():
            # trigger=="*" 表示在时间窗口内持续生效的常驻策略（如夜间自动上锁），
            # 因而会与同一时刻的访客/手动意图在同一次仲裁中正面竞争。
            if rule.trigger != "*" and rule.trigger != event.event_type:
                continue
            if rule.window is not None and event.hour is not None \
                    and not rule.window.contains(event.hour):
                continue
            owner = owner_cache.setdefault(rule.owner_id, self.member(rule.owner_id))
            if owner is None:
                continue
            authority = AUTHORITY_ADMIN if owner.is_admin else AUTHORITY_MEMBER
            for action in rule.actions:
                if replay_devices is not None and action.device_id not in replay_devices:
                    continue
                if not owner.can_control(action.capability):
                    unauthorized.append((action, owner.member_id,
                                         f"{owner.name} 无权控制 {action.capability.value}"))
                    continue
                candidate = Candidate(
                    action=action, source_id=f"rule:{rule.rule_id}",
                    source_name=rule.name, actor_id=owner.member_id,
                    safety_level=rule.safety_level, authority=authority,
                    event_ts=event.ts,
                )
                self._gate_or_deny(event, candidate, rule, denied) or candidates.append(candidate)

    def _collect_visitor_candidate(
        self, event: Event, candidates: list[Candidate],
        denied: list[tuple[Candidate, str]],
        replay_devices: Optional[set[str]],
    ) -> None:
        if event.event_type != "visitor_unlock" or not event.actor_id:
            return
        visitor_pass = self._visitor_pass(event.actor_id)
        lock_device = self._first_device_with(DeviceCapability.LOCK)
        if lock_device is None:
            return
        if replay_devices is not None and lock_device.device_id not in replay_devices:
            return
        action = RuleAction(lock_device.device_id, DeviceCapability.LOCK, "unlock")
        candidate = Candidate(
            action=action, source_id=f"visitor:{event.actor_id}",
            source_name=f"访客凭证 {visitor_pass.visitor_name}" if visitor_pass else "访客凭证",
            actor_id=event.actor_id, safety_level=0,
            authority=AUTHORITY_VISITOR, event_ts=event.ts,
        )
        if visitor_pass is None:
            denied.append((candidate, "访客凭证不存在"))
            return
        if visitor_pass.revoked:
            denied.append((candidate, f"访客 {visitor_pass.visitor_name} 的凭证已被撤销"))
            return
        if not visitor_pass.is_valid(event.ts):
            denied.append((candidate,
                           f"访客凭证不在有效期内（截至 {visitor_pass.valid_until:0.0f}）"))
            return
        if DeviceCapability.LOCK not in visitor_pass.capabilities:
            denied.append((candidate, "访客凭证不含通行能力"))
            return
        candidates.append(candidate)

    def _collect_manual_candidate(
        self, event: Event, candidates: list[Candidate],
        denied: list[tuple[Candidate, str]],
        unauthorized: list[tuple[RuleAction, Optional[str], str]],
        replay_devices: Optional[set[str]],
    ) -> None:
        if event.event_type != "manual_command" or not event.actor_id:
            return
        p = event.payload
        try:
            capability = DeviceCapability(p["capability"])
            action = RuleAction(p["device_id"], capability,
                                p["command"], p.get("params", {}))
        except (KeyError, ValueError):
            return
        if replay_devices is not None and action.device_id not in replay_devices:
            return
        actor = self.member(event.actor_id)
        candidate = Candidate(
            action=action, source_id=f"manual:{event.actor_id}",
            source_name=actor.name if actor else "手动操作",
            actor_id=event.actor_id, safety_level=int(p.get("safety_level", 0)),
            authority=AUTHORITY_ADMIN if (actor and actor.is_admin) else AUTHORITY_MEMBER,
            event_ts=event.ts,
        )
        if actor is None:
            denied.append((candidate, "行为人不是已知家庭成员"))
            return
        if not actor.can_control(capability):
            unauthorized.append((action, actor.member_id,
                                 f"{actor.name} 无权控制 {capability.value}"))
            return
        self._gate_or_deny(event, candidate, None, denied) or candidates.append(candidate)

    # ---- 同意门 ---------------------------------------------------------

    def _gate_or_deny(
        self, event: Event, candidate: Candidate,
        rule: Optional[AutomationRule], denied: list[tuple[Candidate, str]],
    ) -> bool:
        """命中隐私门则登记拒绝并返回 True。

        只有真正读取敏感数据的命令（查看画面/采集/读取健康）才过同意门；
        "停止/关闭"类保护性动作不读取数据，反而加强隐私，故直接放行。
        """
        category = CAPABILITY_DATA.get(candidate.action.capability)
        if category not in (PrivacyCategory.IMAGE, PrivacyCategory.HEALTH):
            return False
        if candidate.action.command in PROTECTIVE_COMMANDS:
            return False
        purpose = event.purpose or (rule.purpose if rule else None)
        subjects = self._subjects_of(event, category)
        try:
            self.gate.authorize_read(candidate.action.capability, subjects, purpose)
        except ConsentViolation as exc:
            denied.append((candidate, f"隐私门拒绝：{exc.reason}"))
            return True
        return False

    def _subjects_of(self, event: Event, category: PrivacyCategory) -> list[str]:
        explicit = event.payload.get("subjects")
        if explicit:
            return list(explicit)
        # 未显式指明时，按该敏感空间内可能被采集到的全体家庭成员处理
        return [m.member_id for m in self.members() if m.role.value != "visitor"]

    @staticmethod
    def _reads_sensitive(capability: DeviceCapability, command: str) -> bool:
        """该动作是否真正读取敏感数据。停止/关闭类保护性动作不算读取。"""
        category = CAPABILITY_DATA.get(capability)
        if category not in (PrivacyCategory.IMAGE, PrivacyCategory.HEALTH):
            return False
        return command not in PROTECTIVE_COMMANDS

    # ---- 执行与降级 -----------------------------------------------------

    def _execute(self, event: Event, winner: Candidate, device: Device,
                 replay_of: Optional[str], command: Optional[str] = None,
                 params: Optional[dict[str, Any]] = None,
                 fallback: bool = False) -> str:
        cmd = command or winner.action.command
        action = Action(
            action_id=new_id("act"), event_id=event.event_id,
            device_id=device.device_id, capability=winner.action.capability,
            command=cmd, params=params if params is not None else winner.action.params,
        )
        self.executed.append(ExecutedCommand(
            action_id=action.action_id, event_id=event.event_id,
            device_id=device.device_id, capability=action.capability,
            command=cmd, params=action.params, fallback=fallback,
        ))
        explanation = self._explain_execution(event, winner, fallback)
        return self._write_action_audit(
            event, device, winner, action.command, action.params,
            ActionOutcome.DEGRADED if fallback else ActionOutcome.EXECUTED,
            explanation, replay_of=replay_of,
        )

    def _handle_degradation(
        self, event: Event, winner: Candidate, device: Device,
        degradation, replay_of: Optional[str],
    ) -> tuple[list[str], bool]:
        ids: list[str] = []
        pending = degradation.retry_on_recovery
        if degradation.fallback_command is not None:
            # 传感器缺失但设备在线：立即执行保守兜底（如夜灯保持点亮、门锁保持闭锁）
            ids.append(self._execute(
                event, winner, device, replay_of,
                command=degradation.fallback_command, params={}, fallback=True,
            ))
        ids.append(self._write_action_audit(
            event, device, winner, degradation.fallback_command, {},
            ActionOutcome.DEGRADED, degradation.explain(),
            detail={"reason": degradation.reason.value}, replay_of=replay_of,
        ))
        return ids, pending

    def recover_device(self, device_id: str,
                       sensors: Optional[frozenset[str]] = None) -> dict[str, Any]:
        """设备升级完成/重新上线/传感器恢复：重放挂起事件并重新仲裁执行。"""
        if self.devices.get(device_id) is None:
            return {"replayed": 0, "entry_ids": []}
        self.devices.recover(device_id, sensors)
        entry_ids: list[str] = []
        remaining: list[dict[str, Any]] = []
        for pending in self._store.list_of("pending_events"):
            degraded: set[str] = set(pending["degraded_devices"])
            if device_id not in degraded:
                remaining.append(pending)
                continue
            event = Event.from_dict(pending["event"])
            outcome = self._process(
                event, replay_devices={device_id}, replay_of=event.event_id)
            entry_ids.extend(outcome["entry_ids"])
            still_pending = set(outcome.get("pending_devices", ()))
            degraded = (degraded - {device_id}) | still_pending
            if degraded:
                pending["degraded_devices"] = sorted(degraded)
                remaining.append(pending)
        self._store.replace_list("pending_events", remaining)
        return {"replayed": 1, "entry_ids": entry_ids}

    # ---- 审计 -----------------------------------------------------------

    def _explain_execution(self, event: Event, winner: Candidate, fallback: bool) -> str:
        base = (
            f"事件 {event.event_type} 触发 {winner.source_name}，"
            f"对 {winner.action.device_id} 执行 {winner.action.command}"
        )
        if fallback:
            return f"{base}（因传感器缺失采用安全兜底动作）"
        return base

    def _write_action_audit(
        self, event: Event, device: Device, winner: Candidate,
        command: Optional[str], params: dict[str, Any],
        outcome: ActionOutcome, explanation: str,
        detail: Optional[dict[str, Any]] = None,
        replay_of: Optional[str] = None,
    ) -> str:
        category = CAPABILITY_DATA.get(winner.action.capability)
        category = category or self._device_category(device)
        subjects = self._subjects_of(event, category) if self._reads_sensitive(
            winner.action.capability, winner.action.command) else []
        rule_id = winner.source_id.split(":", 1)[1] if winner.source_id.startswith("rule:") else None
        rule = self.rules.get(rule_id) if rule_id else None
        # 自动化规则不把规则主人视为"直接操作者"，避免借记录查看他人敏感活动；
        # 手动/访客指令的行为人对自己的动作有完整可见性。
        operator_id = None if rule_id else winner.actor_id
        full, redacted = viewers_for(
            category=category, subjects=subjects, operator_id=operator_id,
            rule_owner_id=rule.owner_id if rule else None,
            admins=[m.member_id for m in self.members() if m.is_admin],
        )
        return self.audit.record(
            event_id=event.event_id, event_type=event.event_type,
            device_id=device.device_id, device_name=device.name,
            capability=winner.action.capability, command=command,
            outcome=outcome, explanation=explanation, category=category,
            actor_id=winner.actor_id, subjects=subjects,
            visible_full=full, visible_redacted=redacted,
            rule_id=rule_id, rule_name=rule.name if rule else None,
            params=params, detail=detail, replay_of=replay_of,
        )

    def _write_decision_audit(
        self, event: Event, candidate: Candidate,
        outcome: ActionOutcome, explanation: str,
    ) -> str:
        device = self.devices.get(candidate.action.device_id)
        category = CAPABILITY_DATA.get(candidate.action.capability)
        category = category or (self._device_category(device) if device else PrivacyCategory.ENVIRONMENT)
        subjects = self._subjects_of(event, category) if self._reads_sensitive(
            candidate.action.capability, candidate.action.command) else []
        rule_id = candidate.source_id.split(":", 1)[1] if candidate.source_id.startswith("rule:") else None
        rule = self.rules.get(rule_id) if rule_id else None
        operator_id = None if rule_id else candidate.actor_id
        full, redacted = viewers_for(
            category=category, subjects=subjects, operator_id=operator_id,
            rule_owner_id=rule.owner_id if rule else None,
            admins=[m.member_id for m in self.members() if m.is_admin],
        )
        return self.audit.record(
            event_id=event.event_id, event_type=event.event_type,
            device_id=candidate.action.device_id,
            device_name=device.name if device else candidate.action.device_id,
            capability=candidate.action.capability,
            command=candidate.action.command, outcome=outcome,
            explanation=explanation, category=category,
            actor_id=candidate.actor_id, subjects=subjects,
            visible_full=full, visible_redacted=redacted,
            rule_id=rule_id, rule_name=rule.name if rule else None,
            params=candidate.action.params,
        )

    def _write_plain_audit(
        self, event: Event, action: RuleAction, outcome: ActionOutcome,
        explanation: str, actor_id: Optional[str],
    ) -> str:
        device = self.devices.get(action.device_id)
        category = CAPABILITY_DATA.get(action.capability)
        category = category or (self._device_category(device) if device else PrivacyCategory.ENVIRONMENT)
        full, redacted = viewers_for(
            category=category, subjects=[], operator_id=actor_id,
            rule_owner_id=None,
            admins=[m.member_id for m in self.members() if m.is_admin],
        )
        return self.audit.record(
            event_id=event.event_id, event_type=event.event_type,
            device_id=action.device_id,
            device_name=device.name if device else action.device_id,
            capability=action.capability, command=action.command,
            outcome=outcome, explanation=explanation, category=category,
            actor_id=actor_id, subjects=[],
            visible_full=full, visible_redacted=redacted,
            params=action.params,
        )

    def _record_dedup(self, event: Event, prior: dict[str, Any]) -> None:
        lock = self._first_device_with(DeviceCapability.LOCK)
        target_device = None
        capability = DeviceCapability.LOCK
        if event.event_type == "manual_command":
            target_device = self.devices.get(event.payload.get("device_id", ""))
            try:
                capability = DeviceCapability(event.payload.get("capability", "lock"))
            except ValueError:
                pass
        device = target_device or lock
        if device is None:
            return
        category = CAPABILITY_DATA.get(capability, PrivacyCategory.PRESENCE)
        admins = [m.member_id for m in self.members() if m.is_admin]
        full, redacted = viewers_for(
            category=category, subjects=[], operator_id=event.actor_id,
            rule_owner_id=None, admins=admins)
        self.audit.record(
            event_id=event.event_id, event_type=event.event_type,
            device_id=device.device_id, device_name=device.name,
            capability=capability, command=None,
            outcome=ActionOutcome.DEDUPED,
            explanation=f"重复事件 {event.event_id} 已被幂等拦截，未再次开锁/断电",
            category=category, actor_id=event.actor_id, subjects=[],
            visible_full=full, visible_redacted=redacted,
        )

    # ---- 挂起队列与幂等台账 --------------------------------------------

    def _enqueue_pending(self, event: Event, devices: set[str]) -> None:
        pending_list = self._store.list_of("pending_events")
        for pending in pending_list:
            if pending["event"]["event_id"] == event.event_id:
                pending["degraded_devices"] = sorted(
                    set(pending["degraded_devices"]) | devices)
                self._store.replace_list("pending_events", pending_list)
                return
        self._store.append("pending_events", {
            "event": event.to_dict(),
            "degraded_devices": sorted(devices),
            "enqueued_at": self._clock(),
        })

    def pending_count(self) -> int:
        return len(self._store.list_of("pending_events"))

    @staticmethod
    def _executed_key(event_id: str, device_id: str, capability: str) -> str:
        return f"{event_id}|{device_id}|{capability}"

    @staticmethod
    def _cap_name(capability) -> str:
        return capability.value if isinstance(capability, DeviceCapability) else str(capability)

    def _executed_keys_of(self, event_id: str) -> list[str]:
        prefix = event_id + "|"
        return sorted(k for k in self._executed_keys if k.startswith(prefix))

    def _mark_executed(self, event_id: str, device_id: str, capability) -> None:
        self._executed_keys.add(
            self._executed_key(event_id, device_id, self._cap_name(capability)))

    def _already_executed(self, event_id: str, device_id: str, capability) -> bool:
        name = self._cap_name(capability)
        # 恢复重放或重启后也要尊重历史台账
        prior = self._store.get("processed_events", event_id)
        if prior and self._executed_key(event_id, device_id, name) \
                in prior.get("executed_keys", []):
            return True
        return self._executed_key(event_id, device_id, name) in self._executed_keys

    # ---- 工具 -----------------------------------------------------------

    def _first_device_with(self, capability: DeviceCapability) -> Optional[Device]:
        for device in self.devices.all_devices():
            if capability in device.capabilities:
                return device
        return None

    @staticmethod
    def _device_category(device: Optional[Device]) -> PrivacyCategory:
        if device and device.privacy_category:
            return device.privacy_category
        return PrivacyCategory.ENVIRONMENT

    def _restore(self) -> None:
        """从本地存储重建设备注册表（运行态默认在线）。"""
        for data in self._store.all("devices"):
            self.devices.register(Device.from_dict(data))
        if not hasattr(self, "_executed_keys"):
            self._executed_keys = set()
