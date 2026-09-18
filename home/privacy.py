"""同意与隐私门：敏感数据只能为获准用途读取，撤回同意立即停止新使用。

* 普通环境数据（温度、照度）不涉及个人敏感信息，可直接读取；
* 摄像画面（IMAGE）与睡眠/健康信息（HEALTH）必须同时满足：
    1. 调用方声明了明确目的 purpose；
    2. 该数据主体就 (类别, 目的) 给出过 GRANTED 同意且未撤回。
* 撤回是"即时生效且只影响新使用"：已落库的历史审计不抹除，但此后新的读取/
  基于该数据的动作一律拒绝。
"""

from __future__ import annotations

from typing import Iterable, Optional

from home.models import (
    CAPABILITY_DATA,
    SENSITIVE_CATEGORIES,
    Consent,
    ConsentStatus,
    DeviceCapability,
    Event,
    PrivacyCategory,
    now_ts,
)
from home.store import LocalStore


class ConsentViolation(Exception):
    """读取/使用敏感数据不满足目的限定或同意要求。"""

    def __init__(self, reason: str, category: Optional[PrivacyCategory] = None,
                 subject_id: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.category = category
        self.subject_id = subject_id


class ConsentGate:
    def __init__(self, store: LocalStore, clock=now_ts):
        self._store = store
        self._clock = clock

    # ---- 同意的授予与撤回 ----------------------------------------------

    def grant(self, subject_id: str, category: PrivacyCategory, purpose: str) -> Consent:
        ts = self._clock()
        existing = self._find(subject_id, category, purpose)
        consent = Consent(
            subject_id=subject_id,
            category=category,
            purpose=purpose,
            status=ConsentStatus.GRANTED,
            granted_at=existing.granted_at if existing else ts,
            updated_at=ts,
        )
        self._upsert(consent)
        return consent

    def withdraw(self, subject_id: str, category: PrivacyCategory, purpose: str) -> Consent:
        ts = self._clock()
        existing = self._find(subject_id, category, purpose)
        consent = Consent(
            subject_id=subject_id,
            category=category,
            purpose=purpose,
            status=ConsentStatus.WITHDRAWN,
            granted_at=existing.granted_at if existing else ts,
            updated_at=ts,
        )
        self._upsert(consent)
        return consent

    def withdraw_all(self, subject_id: str, category: Optional[PrivacyCategory] = None) -> int:
        """撤回某主体的全部（或某类）同意，返回撤回条数。用于家庭成员彻底退出。"""
        count = 0
        for data in self._store.list_of("consents"):
            consent = Consent.from_dict(data)
            if consent.subject_id != subject_id:
                continue
            if category is not None and consent.category != category:
                continue
            if consent.status != ConsentStatus.WITHDRAWN:
                self.withdraw(subject_id, consent.category, consent.purpose)
                count += 1
        return count

    def is_granted(self, subject_id: str, category: PrivacyCategory, purpose: str) -> bool:
        consent = self._find(subject_id, category, purpose)
        return consent is not None and consent.status == ConsentStatus.GRANTED

    def list_for(self, subject_id: str) -> list[Consent]:
        return [
            Consent.from_dict(c)
            for c in self._store.list_of("consents")
            if c["subject_id"] == subject_id
        ]

    # ---- 目的限定读取 ---------------------------------------------------

    @staticmethod
    def category_of(capability: DeviceCapability) -> Optional[PrivacyCategory]:
        return CAPABILITY_DATA.get(capability)

    def authorize_read(
        self,
        capability: DeviceCapability,
        subjects: Iterable[str],
        purpose: Optional[str],
    ) -> None:
        """对一次敏感数据读取做目的+同意校验，不通过抛 ConsentViolation。

        摄像头画面可能涉及多人，因此 subjects 中每个数据主体都必须已授权。
        """
        category = self.category_of(capability)
        if category is None or category not in SENSITIVE_CATEGORIES:
            return  # 环境数据无需同意
        if not purpose:
            raise ConsentViolation("读取敏感数据必须声明用途(purpose)", category=category)
        for subject_id in subjects:
            if not self.is_granted(subject_id, category, purpose):
                raise ConsentViolation(
                    f"主体 {subject_id} 未授权 {category.value} 用于 {purpose} 或已撤回",
                    category=category,
                    subject_id=subject_id,
                )

    def authorize_event(self, event: Event, capability: DeviceCapability,
                        subjects: Iterable[str]) -> None:
        self.authorize_read(capability, subjects, event.purpose)

    # ---- 内部 -----------------------------------------------------------

    def _find(self, subject_id: str, category: PrivacyCategory,
              purpose: str) -> Optional[Consent]:
        for data in self._store.list_of("consents"):
            if (
                data["subject_id"] == subject_id
                and data["category"] == category.value
                and data["purpose"] == purpose
            ):
                return Consent.from_dict(data)
        return None

    def _upsert(self, consent: Consent) -> None:
        records = self._store.list_of("consents")
        key = next(
            (
                i for i, c in enumerate(records)
                if c["subject_id"] == consent.subject_id
                and c["category"] == consent.category.value
                and c["purpose"] == consent.purpose
            ),
            None,
        )
        if key is None:
            self._store.append("consents", consent.to_dict())
        else:
            # 列表型集合：重写整个列表以更新单条
            records[key] = consent.to_dict()
            self._store.replace_list("consents", records)
