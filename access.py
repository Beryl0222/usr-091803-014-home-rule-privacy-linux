"""访客临时通行。

凭证包含明确的生效/失效时间与设备、命令范围：
- 未到生效时间、已过期、已撤销、超出设备/命令范围一律拒绝；
- 凭证全部保存在本地，断网不影响已签发凭证的校验；
- 撤销与使用计数留痕。
"""

from __future__ import annotations

from typing import Optional

from domain import (
    REASON_VISITOR_EXPIRED,
    REASON_VISITOR_NOT_YET,
    REASON_VISITOR_REVOKED,
    REASON_VISITOR_SCOPE,
    VisitorPass,
)
from clock_utils import now_iso, parse_iso


class AccessControl:
    def __init__(self):
        self._passes: dict[str, VisitorPass] = {}

    def issue(self, pass_id: str, display_name: str, device_ids: tuple[str, ...],
              command: str, valid_from: str, valid_to: str, host_id: str,
              created_at: Optional[str] = None) -> VisitorPass:
        if parse_iso(valid_to) <= parse_iso(valid_from):
            raise ValueError("访客凭证失效时间必须晚于生效时间")
        visitor_pass = VisitorPass(
            id=pass_id, display_name=display_name,
            device_ids=tuple(device_ids), command=command,
            valid_from=valid_from, valid_to=valid_to, host_id=host_id,
            created_at=created_at or now_iso(),
        )
        self._passes[pass_id] = visitor_pass
        return visitor_pass

    def revoke(self, pass_id: str) -> VisitorPass:
        if pass_id not in self._passes:
            raise KeyError(f"访客凭证不存在: {pass_id}")
        self._passes[pass_id].revoked = True
        return self._passes[pass_id]

    def check(self, pass_id: str, device_id: str, command: str,
              at: Optional[str] = None) -> Optional[str]:
        """通过返回 None；否则返回拒绝原因码。"""
        moment = at or now_iso()
        visitor_pass = self._passes.get(pass_id)
        if visitor_pass is None:
            return REASON_VISITOR_SCOPE
        if visitor_pass.revoked:
            return REASON_VISITOR_REVOKED
        if moment < visitor_pass.valid_from:
            return REASON_VISITOR_NOT_YET
        if moment > visitor_pass.valid_to:
            return REASON_VISITOR_EXPIRED
        if device_id not in visitor_pass.device_ids or command != visitor_pass.command:
            return REASON_VISITOR_SCOPE
        return None

    def record_use(self, pass_id: str) -> None:
        visitor_pass = self._passes.get(pass_id)
        if visitor_pass is not None:
            visitor_pass.use_count += 1

    def all(self) -> list[VisitorPass]:
        return list(self._passes.values())

    def to_data(self) -> list[dict]:
        return [p.to_dict() for p in self._passes.values()]

    @classmethod
    def from_data(cls, rows: list[dict]) -> "AccessControl":
        control = cls()
        for row in rows or []:
            visitor_pass = VisitorPass.from_dict(row)
            control._passes[visitor_pass.id] = visitor_pass
        return control
