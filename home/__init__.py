"""家庭设备本地编排领域包。

设计原则（与 README 中的需求一一对应）：

* 全部状态本地保存：设备能力、成员同意、自动化规则、访客有效期；
* 多动作竞争时由仲裁器按 安全级别 → 授权范围 → 时间 给出唯一、可解释的决定；
* 固件升级 / 传感器缺失 / 设备离线进入明确的降级与恢复流程；
* 重复事件幂等，不会再次开锁或断电；
* 摄像与健康信息遵循目的限定与同意撤回；
* 规则的每次修改都产生不可变版本；
* 审计记录按成员授权过滤：既能解释自己相关的动作，也无法据此推断他人敏感活动。
"""

from home.arbitration import ArbitrationResult, Arbitrator
from home.audit import AuditQuery, AuditService
from home.models import (
    Action,
    ActionOutcome,
    AutomationRule,
    Consent,
    ConsentStatus,
    Device,
    DeviceCapability,
    DeviceState,
    DeviceStatus,
    Event,
    Member,
    MemberRole,
    PrivacyCategory,
    RuleAction,
    TimeWindow,
    VisitorPass,
)
from home.orchestrator import HomeOrchestrator
from home.privacy import ConsentGate, ConsentViolation
from home.resilience import Degradation, DegradationReason
from home.rules import RuleBook, RuleConflictError
from home.store import LocalStore

__all__ = [
    "Action",
    "ActionOutcome",
    "ArbitrationResult",
    "Arbitrator",
    "AuditQuery",
    "AuditService",
    "AutomationRule",
    "Consent",
    "ConsentGate",
    "ConsentStatus",
    "ConsentViolation",
    "Degradation",
    "DegradationReason",
    "Device",
    "DeviceCapability",
    "DeviceState",
    "DeviceStatus",
    "Event",
    "HomeOrchestrator",
    "LocalStore",
    "Member",
    "MemberRole",
    "PrivacyCategory",
    "RuleAction",
    "RuleBook",
    "RuleConflictError",
    "TimeWindow",
    "VisitorPass",
]
