"""家庭编排聚合服务：组合各治理模块，负责事件入口、持久化与种子场景。

场景设定（三代同堂）：
- 爷爷设定起夜夜灯（SAFETY），妈妈设定睡眠时段关灯（COMFORT）——同动作竞争；
- 爸爸签发访客白天临时开门凭证；门锁另有云端缺省规则——在线访客优先，
  断网时云端规则失效而本地凭证仍可开门；
- 摄像头仅按"家庭安防"用途经同意读取，云端 AI 上传因无同意被拒；
- 睡眠设备读健康数据联动空调需孩子监护人同意，撤回即停；
- 燃气泄漏紧急开门，传感器缺失时按故障安全放行。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from access import AccessControl
from audit import AuditLog
from consent import ConsentRegistry
from devices import DeviceManager
from domain import (
    AuthScope,
    Capability,
    DataType,
    Device,
    DeviceType,
    Event,
    Member,
    Outcome,
    SafetyLevel,
)
from engine import ActionExecutor, Orchestrator, RecordingExecutor
from rules import RuleStore
from storage import LocalStore

STATE_FILE = "household_state"


class Household:
    def __init__(self, data_dir: Optional[str | Path] = None,
                 executor: Optional[ActionExecutor] = None):
        self.members: dict[str, Member] = {}
        self.devices = DeviceManager()
        self.rules = RuleStore()
        self.consents = ConsentRegistry()
        self.access = AccessControl()
        self.executor = executor or RecordingExecutor()
        self.audit = AuditLog(self.access, self.members)
        self.cloud_connected = True
        self._event_counter = 0
        self._orchestrator = Orchestrator(
            members=self.members, devices=self.devices, rules=self.rules,
            consents=self.consents, access=self.access,
            executor=self.executor,
        )
        self._store = LocalStore(data_dir) if data_dir else None
        self._lock = threading.RLock()

    # ---- 事件入口 ----
    def event(self, event_type: str, *, actor_id: Optional[str] = None,
              device_id: Optional[str] = None, channel: str = "local",
              payload: Optional[dict[str, Any]] = None,
              occurred_at: Optional[str] = None,
              event_id: Optional[str] = None,
              dedup_key: Optional[str] = None) -> Event:
        self._event_counter += 1
        return Event(
            id=event_id or f"ev-{self._event_counter}",
            type=event_type,
            occurred_at=occurred_at or _fixed_now(),
            actor_id=actor_id,
            device_id=device_id,
            channel=channel,
            payload=payload or {},
            dedup_key=dedup_key,
        )

    def handle(self, event: Event):
        """处理事件：仲裁、执行、入审计、落盘，全程加锁。"""
        with self._lock:
            decision, replayed = self._orchestrator.process(
                event, self.audit.decisions, cloud_connected=self.cloud_connected
            )
            if not replayed:
                self.audit.append(decision, event, decision.results)
                for winner in decision.winners:
                    if winner.scope == AuthScope.VISITOR and event.actor_id:
                        self.access.record_use(event.actor_id)
                self._persist()
            return decision, replayed

    def set_cloud(self, connected: bool) -> None:
        with self._lock:
            self.cloud_connected = connected
            self._persist()

    # ---- 持久化 ----
    def _persist(self) -> None:
        if self._store is None:
            return
        self._store.save(STATE_FILE, self._snapshot())

    def persist(self) -> None:
        with self._lock:
            self._persist()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "members": [m.to_dict() for m in self.members.values()],
            "devices": self.devices.to_data(),
            "consents": self.consents.to_data(),
            "rules": self.rules.to_data(),
            "passes": self.access.to_data(),
            "audit": self.audit.to_data(),
            "processed": self._orchestrator.processed,
            "event_counter": self._event_counter,
            "cloud_connected": self.cloud_connected,
        }

    def load(self) -> "Household":
        if self._store is None:
            return self
        with self._lock:
            data = self._store.load(STATE_FILE, None)
            if data is None:
                return self
            self.members = {
                m["id"]: Member.from_dict(m) for m in data.get("members", [])
            }
            self.devices = DeviceManager.from_data(data.get("devices", []))
            self.consents = ConsentRegistry.from_data(data.get("consents", []))
            self.rules = RuleStore.from_data(data.get("rules", []))
            self.access = AccessControl.from_data(data.get("passes", []))
            self.audit = AuditLog(self.access, self.members)
            self.audit.load_data(data.get("audit", {}))
            self.cloud_connected = data.get("cloud_connected", True)
            self._event_counter = data.get("event_counter", 0)
            self._orchestrator = Orchestrator(
                members=self.members, devices=self.devices, rules=self.rules,
                consents=self.consents, access=self.access,
                executor=self.executor,
                processed=data.get("processed", {}),
            )
        return self

    # ---- 引导 ----
    @classmethod
    def bootstrap(cls, data_dir: str | Path,
                  executor: Optional[ActionExecutor] = None) -> "Household":
        """有本地状态则恢复，否则初始化种子场景并落盘。"""
        store = LocalStore(data_dir)
        if store.load(STATE_FILE, None) is None:
            return cls.seeded(data_dir=data_dir, executor=executor)
        return cls(data_dir=data_dir, executor=executor).load()

    # ---- 种子场景 ----
    @classmethod
    def seeded(cls, data_dir: Optional[str] = None,
               executor: Optional[ActionExecutor] = None) -> "Household":
        home = cls(data_dir=data_dir, executor=executor)
        with home._lock:
            home._seed_members()
            home._seed_devices()
            home._seed_consents()
            home._seed_rules()
            home._seed_visitor_passes()
            home._persist()
        return home

    def _seed_members(self) -> None:
        for member in (
            Member("grandpa", "周建国", "elder"),
            Member("grandma", "王秀兰", "elder"),
            Member("dad", "周明", "parent"),
            Member("mom", "李娟", "parent"),
            Member("kid", "周小乐", "child"),
        ):
            self.members[member.id] = member

    def _seed_devices(self) -> None:
        self.devices.register(Device(
            id="front_door_lock", name="入户门锁", type=DeviceType.LOCK,
            room="玄关",
            capabilities={
                "unlock": Capability("unlock", SafetyLevel.SAFETY,
                                     local_always=True),
                "lock": Capability("lock", SafetyLevel.SAFETY,
                                   local_always=True),
            },
            available_sensors=(), firmware_version="3.1.0",
        ))
        self.devices.register(Device(
            id="living_camera", name="客厅摄像头", type=DeviceType.CAMERA,
            room="客厅",
            capabilities={
                "stream": Capability("stream", SafetyLevel.SAFETY,
                                     data_type=DataType.VIDEO),
                "snapshot": Capability("snapshot", SafetyLevel.SAFETY,
                                       data_type=DataType.VIDEO),
            },
            available_sensors=("motion",), firmware_version="2.4.1",
        ))
        self.devices.register(Device(
            id="corridor_light", name="走廊夜灯", type=DeviceType.LIGHT,
            room="走廊",
            capabilities={
                "on": Capability("on", SafetyLevel.SAFETY),
                "off": Capability("off", SafetyLevel.COMFORT),
            },
            firmware_version="1.2.0",
        ))
        self.devices.register(Device(
            id="bedroom_ac", name="卧室空调", type=DeviceType.AC, room="主卧",
            capabilities={
                "set_mode": Capability("set_mode", SafetyLevel.COMFORT),
                "power_off": Capability("power_off", SafetyLevel.COMFORT),
            },
            available_sensors=("temperature",), firmware_version="5.0.2",
        ))
        self.devices.register(Device(
            id="sleep_band", name="睡眠手环", type=DeviceType.SLEEP_TRACKER,
            room="主卧",
            capabilities={
                "record_session": Capability(
                    "record_session", SafetyLevel.COMFORT,
                    data_type=DataType.HEALTH,
                    requires_sensors=("heart_rate", "breathing"),
                ),
            },
            available_sensors=("heart_rate", "breathing"),
            firmware_version="1.0.7",
        ))
        self.devices.register(Device(
            id="kitchen_gas", name="厨房燃气传感器", type=DeviceType.GAS_SENSOR,
            room="厨房",
            capabilities={
                "alarm_unlock": Capability("alarm_unlock",
                                           SafetyLevel.EMERGENCY,
                                           local_always=True,
                                           requires_sensors=("gas_level",)),
            },
            available_sensors=("gas_level",), firmware_version="0.9.4",
        ))

    def _seed_consents(self) -> None:
        # 父母代表全家同意：摄像头画面仅用于家庭安防（明确限制用途）
        self.consents.grant(
            "consent-camera-security", subject_id="household",
            data_type=DataType.VIDEO, purposes=("home_security",),
            granted_by="dad", granted_at="2026-09-01T09:00:00",
        )
        # 妈妈代孩子同意：健康数据仅用于睡眠调节
        self.consents.grant(
            "consent-kid-sleep", subject_id="kid",
            data_type=DataType.HEALTH, purposes=("sleep_adjust",),
            granted_by="mom", granted_at="2026-09-01T09:05:00",
        )
        # 爷爷本人同意：健康数据用于睡眠分析
        self.consents.grant(
            "consent-grandpa-sleep", subject_id="grandpa",
            data_type=DataType.HEALTH, purposes=("sleep_adjust",),
            granted_by="grandpa", granted_at="2026-09-01T09:10:00",
        )

    def _seed_rules(self) -> None:
        # 1) 爷爷的起夜夜灯：22:00-06:00 走廊有人即亮（防跌倒，SAFETY）
        self.rules.add(
            rule_id="elder_night_light", name="爷爷起夜夜灯",
            trigger="motion_detected",
            source_device="corridor_sensor",
            conditions={"time_range": {"start": "22:00", "end": "06:00"}},
            device_id="corridor_light", command="on", params={},
            requester_id="grandpa", safety=SafetyLevel.SAFETY,
            scope=AuthScope.EXPLICIT_CONSENT,
            created_at="2026-09-02T20:00:00",
        )
        # 2) 妈妈的睡眠时段关灯（节能舒适，COMFORT）——与夜灯在同一事件竞争
        self.rules.add(
            rule_id="sleep_house_dim", name="睡眠时段走廊关灯",
            trigger="motion_detected",
            source_device="corridor_sensor",
            conditions={"time_range": {"start": "22:30", "end": "05:30"}},
            device_id="corridor_light", command="off", params={},
            requester_id="mom", safety=SafetyLevel.COMFORT,
            scope=AuthScope.ROLE,
            created_at="2026-09-03T20:00:00",
        )
        # 3) 爸爸的摄像头白天安防查看：用途受限 + 全家同意
        self.rules.add(
            rule_id="camera_security_day", name="白天安防查看",
            trigger="camera_view_requested",
            source_device="living_camera",
            conditions={"time_range": {"start": "07:00", "end": "21:00"}},
            device_id="living_camera", command="stream", params={},
            requester_id="dad", safety=SafetyLevel.SAFETY,
            scope=AuthScope.EXPLICIT_CONSENT, purpose="home_security",
            created_at="2026-09-02T21:00:00",
        )
        # 4) 云端 AI 抽帧上传：用途从未获准，应被拒绝
        self.rules.add(
            rule_id="cloud_ai_snapshot", name="云端智能抽帧",
            trigger="camera_view_requested",
            source_device="living_camera", conditions={},
            device_id="living_camera", command="snapshot", params={},
            requester_id="dad", safety=SafetyLevel.CONVENIENCE,
            scope=AuthScope.CLOUD_DEFAULT, origin="cloud",
            purpose="cloud_ai_training",
            created_at="2026-09-04T10:00:00",
        )
        # 5) 访客白天临时开门（凭证授权，时间窗与凭证各自校验）
        self.rules.add(
            rule_id="visitor_daytime_admit", name="访客白天临时通行",
            trigger="door_access_requested",
            source_device="front_door_lock",
            conditions={"time_range": {"start": "08:00", "end": "21:00"}},
            device_id="front_door_lock", command="unlock", params={},
            requester_id="dad", safety=SafetyLevel.SAFETY,
            scope=AuthScope.VISITOR,
            created_at="2026-09-05T18:00:00",
        )
        # 6) 云端缺省锁门：在线时参与竞争，断网时失效让位本地凭证
        self.rules.add(
            rule_id="cloud_door_default", name="云端缺省锁门",
            trigger="door_access_requested",
            source_device="front_door_lock", conditions={},
            device_id="front_door_lock", command="lock", params={},
            requester_id="dad", safety=SafetyLevel.SAFETY,
            scope=AuthScope.CLOUD_DEFAULT, origin="cloud",
            created_at="2026-09-04T10:05:00",
        )
        # 7) 睡眠异常联动空调：读健康数据，需监护人对该用途的同意
        self.rules.add(
            rule_id="sleep_thermal_adjust", name="睡眠异常空调调节",
            trigger="sleep_reading",
            source_device="sleep_band",
            conditions={"sensor": {
                "sensor_device": "sleep_band", "name": "breathing",
                "below": 12,
            }},
            device_id="bedroom_ac", command="set_mode",
            params={"mode": "warm", "temp": 26},
            requester_id="mom", safety=SafetyLevel.COMFORT,
            scope=AuthScope.ROLE,
            data_use={"data_type": "health", "purpose": "sleep_adjust"},
            created_at="2026-09-06T20:00:00",
        )
        # 8) 燃气泄漏紧急开门疏散（EMERGENCY）
        self.rules.add(
            rule_id="gas_leak_evacuate", name="燃气泄漏开门疏散",
            trigger="gas_leak",
            source_device="kitchen_gas",
            conditions={"sensor": {
                "sensor_device": "kitchen_gas", "name": "gas_level",
                "above": 20,
            }},
            device_id="front_door_lock", command="unlock", params={},
            requester_id="dad", safety=SafetyLevel.EMERGENCY,
            scope=AuthScope.EXPLICIT_CONSENT,
            created_at="2026-09-02T22:00:00",
        )

    def _seed_visitor_passes(self) -> None:
        # 周末访客：保洁张阿姨，仅可在周六白天开入户门
        self.access.issue(
            pass_id="pass-cleaner", display_name="保洁张阿姨",
            device_ids=("front_door_lock",), command="unlock",
            valid_from="2026-09-19T08:00:00",
            valid_to="2026-09-19T18:00:00",
            host_id="dad", created_at="2026-09-17T12:00:00",
        )


def _fixed_now() -> str:
    """事件默认时间：由测试/调用方显式传入更稳妥，这里仅作兜底。"""
    from clock_utils import now_iso
    return now_iso()
