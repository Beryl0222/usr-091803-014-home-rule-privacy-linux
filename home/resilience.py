"""降级与恢复：固件升级、传感器缺失、设备离线都有明确处理与恢复路径。

三类不可直接执行的情况：

* OFFLINE            设备本身不可达（断电/没电/故障）——与"家庭断网"不同：
                      断网时本编排器仍在本地运行，门锁控制不依赖云；
* UPGRADING          固件升级中——只允许安全兜底，拒绝在刷写时开锁/断电；
* MISSING_SENSOR     动作依赖的传感器当前缺失——采取保守（偏向安全与隐私）兜底。

降级动作落审计；设备恢复后由编排器重放当时被挂起的事件，重新仲裁再执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from home.models import (
    Device,
    DeviceCapability,
    DeviceState,
    DeviceStatus,
)


class DegradationReason(str, Enum):
    OFFLINE = "offline"
    UPGRADING = "upgrading"
    MISSING_SENSOR = "missing_sensor"


@dataclass(frozen=True)
class Degradation:
    device_id: str
    reason: DegradationReason
    detail: str
    fallback_command: Optional[str]      # 保守兜底命令；None 表示保持现状/不执行
    retry_on_recovery: bool = True

    def explain(self) -> str:
        fb = "保持现状" if self.fallback_command is None else f"兜底执行 {self.fallback_command}"
        return f"{self.device_id} 因 {self.reason.value} 降级：{self.detail}；{fb}"


class DeviceRegistry:
    """设备配置 + 实时运行态。配置持久化，运行态可在内存中更新。"""

    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._states: dict[str, DeviceState] = {}

    def register(self, device: Device) -> None:
        self._devices[device.device_id] = device
        self._states.setdefault(device.device_id, DeviceState())

    def get(self, device_id: str) -> Optional[Device]:
        return self._devices.get(device_id)

    def state(self, device_id: str) -> DeviceState:
        return self._states.get(device_id, DeviceState())

    def all_devices(self) -> list[Device]:
        return list(self._devices.values())

    def set_status(self, device_id: str, status: DeviceStatus) -> None:
        prev = self.state(device_id)
        self._states[device_id] = DeviceState(
            status=status, available_sensors=prev.available_sensors
        )

    def set_sensors(self, device_id: str, sensors: frozenset[str]) -> None:
        prev = self.state(device_id)
        self._states[device_id] = DeviceState(status=prev.status, available_sensors=sensors)

    def recover(self, device_id: str, sensors: Optional[frozenset[str]] = None) -> None:
        """设备恢复在线（升级完成/重新上线）。"""
        prev = self.state(device_id)
        self._states[device_id] = DeviceState(
            status=DeviceStatus.ONLINE,
            available_sensors=sensors if sensors is not None else prev.available_sensors,
        )

    def is_recovered(self, before: DeviceState, after: DeviceState) -> bool:
        return before != after and after.status == DeviceStatus.ONLINE


class ResiliencePolicy:
    """判断动作能否下发，不能时给出明确的降级兜底。"""

    def evaluate(
        self,
        device: Device,
        state: DeviceState,
        command: str,
        event_sensors: frozenset[str],
    ) -> Optional[Degradation]:
        if state.status == DeviceStatus.OFFLINE:
            return Degradation(
                device.device_id,
                DegradationReason.OFFLINE,
                "设备离线，指令无法送达，已挂起等待恢复",
                fallback_command=None,
            )
        if state.status == DeviceStatus.UPGRADING:
            return Degradation(
                device.device_id,
                DegradationReason.UPGRADING,
                "固件升级中，禁止开锁/断电等变更，保持安全状态",
                fallback_command=None,
            )
        missing = device.requires_sensors - (state.available_sensors | event_sensors)
        if missing:
            return Degradation(
                device.device_id,
                DegradationReason.MISSING_SENSOR,
                f"缺少传感器：{', '.join(sorted(missing))}",
                fallback_command=self._safe_fallback(device, command),
            )
        return None

    @staticmethod
    def _safe_fallback(device: Device, intended: str) -> Optional[str]:
        """缺传感器时的保守兜底：优先人身安全与隐私，不做激进变更。"""
        cap = next(iter(device.capabilities), None)
        if DeviceCapability.LOCK in device.capabilities:
            # 无法确认环境时不要开锁
            return "lock" if intended == "unlock" else None
        if DeviceCapability.LIGHT in device.capabilities and device.safety_critical:
            # 老人夜间照明缺感应：宁可亮着，避免跌倒
            return "on"
        if DeviceCapability.POWER in device.capabilities or DeviceCapability.CLIMATE in device.capabilities:
            # 不依据不完整信息断电
            return None if intended in ("off", "power_off") else intended
        if DeviceCapability.CAMERA_STREAM in device.capabilities:
            # 隐私保护：无法确认授权语境时不推流
            return None
        return None
