"""领域对象：家庭设备编排涉及的全部核心概念。

设计原则：
- 所有对象都可 JSON 序列化，能完整保存在本地（断网时不依赖云端）。
- 枚举值使用稳定字符串，落盘后跨版本可读。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class SafetyLevel(str, Enum):
    """动作安全级别，仲裁时从高到低比较。"""

    EMERGENCY = "emergency"        # 紧急安全（燃气报警、紧急求助）
    SAFETY = "safety"              # 人身/安防（门锁、布防）
    COMFORT = "comfort"            # 舒适（空调、照明）
    CONVENIENCE = "convenience"    # 便利（情景模式、统计）

    @property
    def rank(self) -> int:
        return list(type(self).__members__.values()).index(self)


class AuthScope(str, Enum):
    """授权范围：越具体的授权在冲突中越优先。"""

    EMERGENCY = "emergency"            # 紧急授权（最高）
    EXPLICIT_CONSENT = "explicit"      # 针对本人/本用途的明确同意
    ROLE = "role"                      # 角色授权（长辈/父母）
    VISITOR = "visitor"                # 访客临时授权
    CLOUD_DEFAULT = "cloud_default"    # 云端下发的缺省授权（最弱）

    @property
    def rank(self) -> int:
        return list(type(self).__members__.values()).index(self)


class DeviceType(str, Enum):
    LOCK = "lock"
    CAMERA = "camera"
    AC = "ac"
    LIGHT = "light"
    SLEEP_TRACKER = "sleep_tracker"
    GAS_SENSOR = "gas_sensor"


class DataType(str, Enum):
    """敏感数据类型，读取需要对应用途与同意。"""

    VIDEO = "video"
    HEALTH = "health"


class DeviceState(str, Enum):
    """设备降级/恢复状态机。"""

    NORMAL = "normal"
    DEGRADED = "degraded"        # 传感器缺失等：仅保留部分能力
    UPDATING = "updating"        # 固件升级中
    RECOVERING = "recovering"    # 升级后重验能力
    OFFLINE = "offline"          # 心跳超时


class RuleStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"     # 被新版本取代，保留可追溯


class ConsentStatus(str, Enum):
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"


class Outcome(str, Enum):
    EXECUTED = "executed"
    SUPPRESSED = "suppressed"     # 在仲裁中落败，未执行
    DENIED = "denied"             # 未通过授权/条件/设备检查


# 抑制/拒绝的原因码（可解释、可测试）
REASON_CLOUD_OFFLINE = "cloud_rule_unavailable_offline"
REASON_RULE_DISABLED = "rule_disabled"
REASON_CONDITION_UNMET = "condition_unmet"
REASON_CONDITION_UNKNOWN = "condition_sensor_missing"
REASON_CONSENT_MISSING = "consent_missing"
REASON_CONSENT_WITHDRAWN = "consent_withdrawn"
REASON_SCOPE_DENIED = "actor_out_of_scope"
REASON_VISITOR_EXPIRED = "visitor_pass_expired"
REASON_VISITOR_NOT_YET = "visitor_pass_not_yet_valid"
REASON_VISITOR_REVOKED = "visitor_pass_revoked"
REASON_VISITOR_SCOPE = "visitor_pass_scope_mismatch"
REASON_DEVICE_UPDATING = "device_updating"
REASON_DEVICE_RECOVERING = "device_recovering_unverified"
REASON_DEVICE_DEGRADED = "capability_unavailable_degraded"
REASON_DEVICE_OFFLINE = "device_offline"
REASON_CAPABILITY_UNKNOWN = "capability_not_declared"
REASON_LOST_CONFLICT = "lost_conflict"
REASON_DUPLICATE = "duplicate_event"


@dataclass
class Member:
    id: str
    name: str
    role: str  # elder / parent / child / visitor / nurse

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Member":
        return cls(id=data["id"], name=data["name"], role=data["role"])


@dataclass
class Capability:
    """设备单项能力：命令、安全级别、是否读取敏感数据、断网/升级时是否本地保留。"""

    command: str
    safety: SafetyLevel
    data_type: Optional[DataType] = None
    local_always: bool = False       # 断网/升级时本地通道仍可执行（如门锁键盘开锁）
    requires_sensors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "safety": self.safety.value,
            "data_type": self.data_type.value if self.data_type else None,
            "local_always": self.local_always,
            "requires_sensors": list(self.requires_sensors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Capability":
        return cls(
            command=data["command"],
            safety=SafetyLevel(data["safety"]),
            data_type=DataType(data["data_type"]) if data.get("data_type") else None,
            local_always=data.get("local_always", False),
            requires_sensors=tuple(data.get("requires_sensors", [])),
        )


@dataclass
class Device:
    id: str
    name: str
    type: DeviceType
    capabilities: dict[str, Capability]
    room: str = ""
    online: bool = True
    state: DeviceState = DeviceState.NORMAL
    firmware_version: str = "1.0.0"
    available_sensors: tuple[str, ...] = ()
    last_heartbeat: Optional[str] = None
    # 升级前缓存的版本，升级失败/恢复完成前可追溯
    previous_firmware: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "room": self.room,
            "capabilities": {k: c.to_dict() for k, c in self.capabilities.items()},
            "online": self.online,
            "state": self.state.value,
            "firmware_version": self.firmware_version,
            "available_sensors": list(self.available_sensors),
            "last_heartbeat": self.last_heartbeat,
            "previous_firmware": self.previous_firmware,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Device":
        return cls(
            id=data["id"],
            name=data["name"],
            type=DeviceType(data["type"]),
            room=data.get("room", ""),
            capabilities={
                k: Capability.from_dict(v) for k, v in data["capabilities"].items()
            },
            online=data.get("online", True),
            state=DeviceState(data.get("state", DeviceState.NORMAL.value)),
            firmware_version=data.get("firmware_version", "1.0.0"),
            available_sensors=tuple(data.get("available_sensors", [])),
            last_heartbeat=data.get("last_heartbeat"),
            previous_firmware=data.get("previous_firmware"),
        )


@dataclass
class Consent:
    """成员（或监护人代孩子）对某类数据、某些用途的授权。"""

    id: str
    subject_id: str           # 被采集数据的成员
    data_type: DataType
    purposes: tuple[str, ...]
    granted_by: str
    granted_at: str
    status: ConsentStatus = ConsentStatus.GRANTED
    withdrawn_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject_id": self.subject_id,
            "data_type": self.data_type.value,
            "purposes": list(self.purposes),
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "status": self.status.value,
            "withdrawn_at": self.withdrawn_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Consent":
        return cls(
            id=data["id"],
            subject_id=data["subject_id"],
            data_type=DataType(data["data_type"]),
            purposes=tuple(data["purposes"]),
            granted_by=data["granted_by"],
            granted_at=data["granted_at"],
            status=ConsentStatus(data.get("status", ConsentStatus.GRANTED.value)),
            withdrawn_at=data.get("withdrawn_at"),
        )


@dataclass
class Rule:
    """一条自动化规则；任何修改都产生新版本，旧版本置为 SUPERSEDED。"""

    rule_id: str
    version: int
    name: str
    trigger: str                       # 事件类型
    conditions: dict[str, Any]         # time_range / device_states / actor_roles ...
    device_id: str
    command: str
    params: dict[str, Any]
    requester_id: str
    safety: SafetyLevel
    scope: AuthScope
    origin: str = "local"              # local / cloud
    purpose: Optional[str] = None      # 读取敏感数据时必须声明
    # 规则条件/动作对敏感数据的额外使用，如 {"data_type": "health", "purpose": "sleep_adjust"}
    data_use: Optional[dict[str, str]] = None
    status: RuleStatus = RuleStatus.ACTIVE
    created_at: str = ""
    parent_version: Optional[int] = None
    source_device: Optional[str] = None  # 触发事件须来自该设备；None 表示不限

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "name": self.name,
            "trigger": self.trigger,
            "conditions": self.conditions,
            "device_id": self.device_id,
            "command": self.command,
            "params": self.params,
            "requester_id": self.requester_id,
            "safety": self.safety.value,
            "scope": self.scope.value,
            "origin": self.origin,
            "purpose": self.purpose,
            "data_use": self.data_use,
            "status": self.status.value,
            "created_at": self.created_at,
            "parent_version": self.parent_version,
            "source_device": self.source_device,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return cls(
            rule_id=data["rule_id"],
            version=data["version"],
            name=data["name"],
            trigger=data["trigger"],
            conditions=data["condition"] if "condition" in data else data["conditions"],
            device_id=data["device_id"],
            command=data["command"],
            params=data.get("params", {}),
            requester_id=data["requester_id"],
            safety=SafetyLevel(data["safety"]),
            scope=AuthScope(data["scope"]),
            origin=data.get("origin", "local"),
            purpose=data.get("purpose"),
            data_use=data.get("data_use"),
            status=RuleStatus(data.get("status", RuleStatus.ACTIVE.value)),
            created_at=data.get("created_at", ""),
            parent_version=data.get("parent_version"),
            source_device=data.get("source_device"),
        )


@dataclass
class VisitorPass:
    """访客临时通行凭证：有效期 + 授权范围，过期自动失效。"""

    id: str
    display_name: str
    device_ids: tuple[str, ...]
    command: str
    valid_from: str
    valid_to: str
    host_id: str
    revoked: bool = False
    use_count: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisitorPass":
        return cls(
            id=data["id"],
            display_name=data["display_name"],
            device_ids=tuple(data["device_ids"]),
            command=data["command"],
            valid_from=data["valid_from"],
            valid_to=data["valid_to"],
            host_id=data["host_id"],
            revoked=data.get("revoked", False),
            use_count=data.get("use_count", 0),
            created_at=data.get("created_at", ""),
        )


@dataclass
class Event:
    id: str
    type: str
    occurred_at: str
    actor_id: Optional[str] = None
    device_id: Optional[str] = None
    channel: str = "local"             # local / remote
    payload: dict[str, Any] = field(default_factory=dict)
    dedup_key: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            id=data["id"],
            type=data["type"],
            occurred_at=data["occurred_at"],
            actor_id=data.get("actor_id"),
            device_id=data.get("device_id"),
            channel=data.get("channel", "local"),
            payload=data.get("payload", {}),
            dedup_key=data.get("dedup_key"),
        )


@dataclass
class CandidateResult:
    """每个候选动作的评估结果。"""

    rule_id: str
    rule_version: int
    device_id: str
    command: str
    requester_id: str
    safety: SafetyLevel
    scope: AuthScope
    outcome: Outcome
    rank: Optional[int] = None
    reasons: tuple[str, ...] = ()
    mode: Optional[str] = None         # local / cloud，仅执行时有意义
    sensitive: bool = False
    purpose: Optional[str] = None
    notes: tuple[str, ...] = ()        # 信息性说明（如紧急动作在传感器缺失下放行）

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "device_id": self.device_id,
            "command": self.command,
            "requester_id": self.requester_id,
            "safety": self.safety.value,
            "scope": self.scope.value,
            "outcome": self.outcome.value,
            "rank": self.rank,
            "reasons": list(self.reasons),
            "mode": self.mode,
            "sensitive": self.sensitive,
            "purpose": self.purpose,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateResult":
        return cls(
            rule_id=data["rule_id"],
            rule_version=data["rule_version"],
            device_id=data["device_id"],
            command=data["command"],
            requester_id=data["requester_id"],
            safety=SafetyLevel(data["safety"]),
            scope=AuthScope(data["scope"]),
            outcome=Outcome(data["outcome"]),
            rank=data.get("rank"),
            reasons=tuple(data.get("reasons", [])),
            mode=data.get("mode"),
            sensitive=data.get("sensitive", False),
            purpose=data.get("purpose"),
            notes=tuple(data.get("notes", [])),
        )


@dataclass
class Decision:
    """一次事件的唯一仲裁决定。"""

    id: str
    event_id: str
    event_type: str
    dedup_fingerprint: str
    decided_at: str
    cloud_connected: bool
    results: list[CandidateResult]
    winner_keys: list[str] = field(default_factory=list)  # ["rule_id:version"]，每台冲突设备一个
    replayed: bool = False             # 重复事件回放，未再次执行

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "dedup_fingerprint": self.dedup_fingerprint,
            "decided_at": self.decided_at,
            "cloud_connected": self.cloud_connected,
            "winner_keys": self.winner_keys,
            "replayed": self.replayed,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        winner_keys = data.get("winner_keys")
        if winner_keys is None:
            # 兼容早期单获胜者格式
            winner_keys = [data["winner_key"]] if data.get("winner_key") else []
        return cls(
            id=data["id"],
            event_id=data["event_id"],
            event_type=data.get("event_type", ""),
            dedup_fingerprint=data["dedup_fingerprint"],
            decided_at=data["decided_at"],
            cloud_connected=data["cloud_connected"],
            winner_keys=winner_keys,
            replayed=data.get("replayed", False),
            results=[CandidateResult.from_dict(r) for r in data["results"]],
        )

    @property
    def winners(self) -> list[CandidateResult]:
        keys = set(self.winner_keys)
        return [r for r in self.results
                if f"{r.rule_id}:{r.rule_version}" in keys]

    @property
    def winner(self) -> Optional[CandidateResult]:
        winners = self.winners
        return winners[0] if winners else None
