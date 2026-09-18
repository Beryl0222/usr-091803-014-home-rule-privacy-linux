"""领域对象：成员、设备、同意、规则、访客、事件与执行结果。

这些对象都是可 JSON 序列化的值对象，便于本地持久化与离线使用；
不依赖任何云端服务。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def now_ts() -> float:
    """统一的时间来源（UTC 秒），测试时可通过注入时钟替换。"""
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class MemberRole(str, Enum):
    """家庭角色。角色决定默认授权范围，具体授权仍以成员记录为准。"""

    ELDER = "elder"          # 祖辈老人
    PARENT = "parent"        # 父母（家庭管理员）
    CHILD = "child"          # 孩子
    VISITOR = "visitor"      # 访客（只能使用自己的临时凭证）


# 可作用于设备的能力。设备只拥有自己声明过的能力。
class DeviceCapability(str, Enum):
    LOCK = "lock"                 # 门锁开锁/闭锁
    CAMERA_CAPTURE = "camera"    # 摄像画面采集
    CAMERA_STREAM = "stream"     # 实时画面查看
    LIGHT = "light"               # 照明
    CLIMATE = "climate"           # 空调调温/开关
    SLEEP_MONITOR = "sleep"       # 睡眠/健康监测
    POWER = "power"               # 设备电源通断（空调等的"断电"）


# 数据的隐私类别：普通环境数据 vs 敏感数据，读取需目的+同意。
class PrivacyCategory(str, Enum):
    ENVIRONMENT = "environment"   # 温度、照度等
    PRESENCE = "presence"         # 在场/通行事件
    IMAGE = "image"               # 摄像画面
    HEALTH = "health"             # 睡眠/健康信息


# 需要目的限定的敏感类别
SENSITIVE_CATEGORIES = frozenset({PrivacyCategory.IMAGE, PrivacyCategory.HEALTH})

# 能力会产生/读取的数据类别
CAPABILITY_DATA: dict[DeviceCapability, PrivacyCategory] = {
    DeviceCapability.LOCK: PrivacyCategory.PRESENCE,
    DeviceCapability.CAMERA_CAPTURE: PrivacyCategory.IMAGE,
    DeviceCapability.CAMERA_STREAM: PrivacyCategory.IMAGE,
    DeviceCapability.SLEEP_MONITOR: PrivacyCategory.HEALTH,
}


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UPGRADING = "upgrading"   # 固件升级中：只接受安全兜底策略


class ConsentStatus(str, Enum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    role: MemberRole
    # 该成员可管理的设备能力，例如 {LIGHT, CLIMATE}；管理员可为全部
    capabilities: frozenset[DeviceCapability] = frozenset()
    is_admin: bool = False

    def can_control(self, capability: DeviceCapability) -> bool:
        return self.is_admin or capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "name": self.name,
            "role": self.role.value,
            "capabilities": sorted(c.value for c in self.capabilities),
            "is_admin": self.is_admin,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Member":
        return cls(
            member_id=data["member_id"],
            name=data["name"],
            role=MemberRole(data["role"]),
            capabilities=frozenset(DeviceCapability(c) for c in data.get("capabilities", [])),
            is_admin=data.get("is_admin", False),
        )


@dataclass(frozen=True)
class TimeWindow:
    """[start_hour, end_hour) 的本地作息窗口，支持跨午夜（如 22→6）。"""

    start_hour: int
    end_hour: int

    def contains(self, hour: int) -> bool:
        if self.start_hour == self.end_hour:
            return True  # 全天
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def to_dict(self) -> dict[str, Any]:
        return {"start_hour": self.start_hour, "end_hour": self.end_hour}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimeWindow":
        return cls(start_hour=data["start_hour"], end_hour=data["end_hour"])


@dataclass(frozen=True)
class Device:
    device_id: str
    name: str
    capabilities: frozenset[DeviceCapability]
    # 该设备执行动作所需的传感器前提；传感器缺失时进入降级
    requires_sensors: frozenset[str] = frozenset()
    # 断电会影响的安全相关能力（例如空调断电不影响安全，照明断电影响跌倒风险）
    safety_critical: bool = False
    # 该设备是否可能读取敏感数据（摄像头/睡眠带）
    privacy_category: Optional[PrivacyCategory] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "requires_sensors": sorted(self.requires_sensors),
            "safety_critical": self.safety_critical,
            "privacy_category": self.privacy_category.value if self.privacy_category else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Device":
        return cls(
            device_id=data["device_id"],
            name=data["name"],
            capabilities=frozenset(DeviceCapability(c) for c in data["capabilities"]),
            requires_sensors=frozenset(data.get("requires_sensors", [])),
            safety_critical=data.get("safety_critical", False),
            privacy_category=(
                PrivacyCategory(data["privacy_category"]) if data.get("privacy_category") else None
            ),
        )


@dataclass(frozen=True)
class Consent:
    """某数据主体对某类数据用于某目的的同意。撤回后停止新的使用。"""

    subject_id: str                 # 数据被采集的成员（通常是老人/孩子）
    category: PrivacyCategory
    purpose: str                    # 例如 "night_safety" / "guest_verification"
    status: ConsentStatus
    granted_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "category": self.category.value,
            "purpose": self.purpose,
            "status": self.status.value,
            "granted_at": self.granted_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Consent":
        return cls(
            subject_id=data["subject_id"],
            category=PrivacyCategory(data["category"]),
            purpose=data["purpose"],
            status=ConsentStatus(data["status"]),
            granted_at=data["granted_at"],
            updated_at=data["updated_at"],
        )


@dataclass(frozen=True)
class RuleAction:
    """规则触发后要对设备执行的动作。

    on_conflict 声明该动作在竞争中的立场，仲裁器据此产出唯一决定。
    """

    device_id: str
    capability: DeviceCapability
    command: str                     # unlock / lock / on / off / set / stream ...
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "capability": self.capability.value,
            "command": self.command,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleAction":
        return cls(
            device_id=data["device_id"],
            capability=DeviceCapability(data["capability"]),
            command=data["command"],
            params=data.get("params", {}),
        )

    def conflicts_with(self, other: "RuleAction") -> bool:
        """同一设备同一能力上发出不同命令即构成竞争。"""
        if self.device_id != other.device_id or self.capability != other.capability:
            return False
        if self.command != other.command:
            return True
        # 同为 set 但目标值不同（如空调温度）也算冲突
        return self.params != other.params


@dataclass(frozen=True)
class AutomationRule:
    rule_id: str
    name: str
    owner_id: str                    # 创建者
    trigger: str                     # 事件类型，如 "night_window" / "visitor_arrival"
    actions: tuple[RuleAction, ...]
    # 仅在时间窗口内生效；None 表示不限时间
    window: Optional[TimeWindow] = None
    # 读取敏感数据所声明的目的（摄像头/健康），需与同意记录匹配
    purpose: Optional[str] = None
    # 安全级别：数值越大越优先（见 arbitration）
    safety_level: int = 0
    enabled: bool = True
    version: int = 1
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "owner_id": self.owner_id,
            "trigger": self.trigger,
            "actions": [a.to_dict() for a in self.actions],
            "window": self.window.to_dict() if self.window else None,
            "purpose": self.purpose,
            "safety_level": self.safety_level,
            "enabled": self.enabled,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutomationRule":
        return cls(
            rule_id=data["rule_id"],
            name=data["name"],
            owner_id=data["owner_id"],
            trigger=data["trigger"],
            actions=tuple(RuleAction.from_dict(a) for a in data["actions"]),
            window=TimeWindow.from_dict(data["window"]) if data.get("window") else None,
            purpose=data.get("purpose"),
            safety_level=data.get("safety_level", 0),
            enabled=data.get("enabled", True),
            version=data.get("version", 1),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )


@dataclass(frozen=True)
class VisitorPass:
    """访客临时通行凭证：有明确有效期，过期自动失效。"""

    pass_id: str
    visitor_name: str
    capabilities: frozenset[DeviceCapability]   # 通常只有 LOCK
    valid_from: float
    valid_until: float
    revoked: bool = False
    used_event_ids: tuple[str, ...] = ()        # 已消费的事件（幂等）

    def is_valid(self, ts: float) -> bool:
        return not self.revoked and self.valid_from <= ts < self.valid_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_id": self.pass_id,
            "visitor_name": self.visitor_name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "revoked": self.revoked,
            "used_event_ids": list(self.used_event_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisitorPass":
        return cls(
            pass_id=data["pass_id"],
            visitor_name=data["visitor_name"],
            capabilities=frozenset(DeviceCapability(c) for c in data.get("capabilities", [])),
            valid_from=data["valid_from"],
            valid_until=data["valid_until"],
            revoked=data.get("revoked", False),
            used_event_ids=tuple(data.get("used_event_ids", [])),
        )


@dataclass(frozen=True)
class Event:
    """进入编排系统的事件。event_id 用于去重，保证幂等。"""

    event_id: str
    event_type: str
    ts: float
    actor_id: Optional[str] = None      # 成员或访客凭证
    hour: Optional[int] = None          # 事件发生的本地小时，用于窗口匹配
    sensors_present: frozenset[str] = frozenset()
    payload: dict[str, Any] = field(default_factory=dict)
    purpose: Optional[str] = None       # 读取敏感数据时声明的目的

    @staticmethod
    def make(
        event_type: str,
        ts: Optional[float] = None,
        actor_id: Optional[str] = None,
        hour: Optional[int] = None,
        sensors_present: Optional[frozenset[str]] = None,
        payload: Optional[dict[str, Any]] = None,
        purpose: Optional[str] = None,
    ) -> "Event":
        ts = now_ts() if ts is None else ts
        return Event(
            event_id=new_id("evt"),
            event_type=event_type,
            ts=ts,
            actor_id=actor_id,
            hour=hour if hour is not None else datetime.fromtimestamp(ts, tz=timezone.utc).hour,
            sensors_present=sensors_present if sensors_present is not None else frozenset(),
            payload=payload or {},
            purpose=purpose,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "ts": self.ts,
            "actor_id": self.actor_id,
            "hour": self.hour,
            "sensors_present": sorted(self.sensors_present),
            "payload": self.payload,
            "purpose": self.purpose,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            ts=data["ts"],
            actor_id=data.get("actor_id"),
            hour=data.get("hour"),
            sensors_present=frozenset(data.get("sensors_present", [])),
            payload=data.get("payload", {}),
            purpose=data.get("purpose"),
        )


@dataclass(frozen=True)
class Action:
    """仲裁后真正下发给设备的动作（含决定理由）。"""

    action_id: str
    event_id: str
    device_id: str
    capability: DeviceCapability
    command: str
    params: dict[str, Any] = field(default_factory=dict)


class ActionOutcome(str, Enum):
    EXECUTED = "executed"
    SUPPRESSED = "suppressed"   # 仲裁落败或无授权
    DEGRADED = "degraded"       # 进入降级（离线/升级/缺传感器）
    DENIED = "denied"           # 同意/隐私门拒绝
    DEDUPED = "deduped"         # 重复事件，不再执行


@dataclass(frozen=True)
class DeviceState:
    """设备运行态（非持久配置）：在线情况、固件升级、可用传感器。"""

    status: DeviceStatus = DeviceStatus.ONLINE
    available_sensors: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "available_sensors": sorted(self.available_sensors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceState":
        return cls(
            status=DeviceStatus(data.get("status", DeviceStatus.ONLINE.value)),
            available_sensors=frozenset(data.get("available_sensors", [])),
        )
