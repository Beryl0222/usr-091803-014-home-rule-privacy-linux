"""成员同意治理。

- 同意按 数据类型 + 用途 授权，摄像/健康读取必须同时满足两者。
- 撤回立即生效：撤回之后的新读取一律拒绝（撤回前已完成的动作记录保留）。
- 撤回只停止新的使用，不删除历史审计（历史仍受可见性约束）。
"""

from __future__ import annotations

from domain import Consent, ConsentStatus, DataType
from clock_utils import now_iso


class ConsentRegistry:
    def __init__(self):
        self._consents: dict[str, Consent] = {}

    # ---- 管理 ----
    def grant(self, consent_id: str, subject_id: str, data_type: DataType,
              purposes: tuple[str, ...], granted_by: str,
              granted_at: str | None = None) -> Consent:
        consent = Consent(
            id=consent_id,
            subject_id=subject_id,
            data_type=data_type,
            purposes=tuple(purposes),
            granted_by=granted_by,
            granted_at=granted_at or now_iso(),
        )
        self._consents[consent_id] = consent
        return consent

    def withdraw(self, consent_id: str, withdrawn_at: str | None = None) -> Consent:
        if consent_id not in self._consents:
            raise KeyError(f"同意不存在: {consent_id}")
        consent = self._consents[consent_id]
        consent.status = ConsentStatus.WITHDRAWN
        consent.withdrawn_at = withdrawn_at or now_iso()
        return consent

    # ---- 查询 ----
    def permits(self, subject_id: str, data_type: DataType,
                purpose: str, at: str | None = None) -> bool:
        """给定数据主体、数据类型与用途，在 at 时刻是否存在有效同意。"""
        allowed, _ = self.check(subject_id, data_type, purpose, at)
        return allowed

    def check(self, subject_id: str, data_type: DataType,
              purpose: str, at: str | None = None) -> tuple[bool, str | None]:
        """返回 (是否允许, 拒绝原因码)；撤回与从未授权明确区分。"""
        from domain import REASON_CONSENT_MISSING, REASON_CONSENT_WITHDRAWN
        ever_granted = False
        for consent in self._consents.values():
            if (consent.subject_id == subject_id
                    and consent.data_type == data_type
                    and purpose in consent.purposes):
                if consent.status == ConsentStatus.GRANTED and (
                        at is None or consent.granted_at <= at):
                    return True, None
                ever_granted = True
        if ever_granted:
            return False, REASON_CONSENT_WITHDRAWN
        return False, REASON_CONSENT_MISSING

    def all(self) -> list[Consent]:
        return list(self._consents.values())

    def to_data(self) -> list[dict]:
        return [c.to_dict() for c in self._consents.values()]

    @classmethod
    def from_data(cls, rows: list[dict]) -> "ConsentRegistry":
        registry = cls()
        for row in rows or []:
            consent = Consent.from_dict(row)
            registry._consents[consent.id] = consent
        return registry
