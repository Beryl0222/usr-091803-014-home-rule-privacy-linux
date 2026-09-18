"""设备生命周期：固件升级、传感器缺失与离线的降级/恢复。

显式状态机（写入 domain.DeviceState）：
    normal ──begin_update──▶ updating ──update_finished──▶ recovering
    recovering ──verify_capabilities──▶ normal / degraded
    normal/degraded ──sensor_lost──▶ degraded；degraded ──sensor_restored──▶ normal
    任意在线状态 ──heartbeat_lost──▶ offline；offline ──heartbeat──▶ recovering

关键不变量：
- updating 期间除 local_always 外一律不执行命令（不能边升级边断电/开锁）；
- degraded 设备只能执行"不依赖缺失传感器"的能力；
- offline 时仅 local_always 能力经本地通道可用，门锁因此不会因断网失联；
- 升级失败可回滚到 previous_firmware，回滚后同样进入 recovering 重验。
"""

from __future__ import annotations

from typing import Optional

from domain import (
    Capability,
    Device,
    DeviceState,
    REASON_DEVICE_DEGRADED,
    REASON_DEVICE_OFFLINE,
    REASON_DEVICE_RECOVERING,
    REASON_DEVICE_UPDATING,
)
from clock_utils import now_iso


class DeviceError(ValueError):
    pass


class DeviceManager:
    def __init__(self):
        self.devices: dict[str, Device] = {}

    # ---- 注册/查询 ----
    def register(self, device: Device) -> Device:
        self.devices[device.id] = device
        return device

    def get(self, device_id: str) -> Device:
        return self.devices[device_id]

    # ---- 固件升级 ----
    def begin_update(self, device_id: str, target_version: str,
                     at: Optional[str] = None) -> Device:
        device = self.get(device_id)
        if device.state == DeviceState.UPDATING:
            raise DeviceError(f"设备升级中: {device_id}")
        if not device.online:
            raise DeviceError(f"设备离线，无法升级: {device_id}")
        device.previous_firmware = device.firmware_version
        device.state = DeviceState.UPDATING
        device.last_heartbeat = at or now_iso()
        device.firmware_version = f"{target_version} (updating)"
        return device

    def update_finished(self, device_id: str, target_version: str,
                        at: Optional[str] = None) -> Device:
        device = self.get(device_id)
        if device.state != DeviceState.UPDATING:
            raise DeviceError(f"设备不在升级中: {device_id}")
        device.firmware_version = target_version
        device.state = DeviceState.RECOVERING
        device.last_heartbeat = at or now_iso()
        return device

    def update_failed(self, device_id: str, at: Optional[str] = None) -> Device:
        """升级失败：回滚固件，进入恢复重验。"""
        device = self.get(device_id)
        if device.state != DeviceState.UPDATING:
            raise DeviceError(f"设备不在升级中: {device_id}")
        if device.previous_firmware:
            device.firmware_version = device.previous_firmware
        device.state = DeviceState.RECOVERING
        device.last_heartbeat = at or now_iso()
        return device

    # ---- 能力重验（升级后/重连后必经） ----
    def verify_capabilities(self, device_id: str,
                            available_sensors: tuple[str, ...],
                            at: Optional[str] = None) -> Device:
        device = self.get(device_id)
        if device.state not in (DeviceState.RECOVERING, DeviceState.DEGRADED):
            raise DeviceError(f"设备当前状态无需重验: {device_id}（{device.state.value}）")
        device.available_sensors = tuple(available_sensors)
        device.last_heartbeat = at or now_iso()
        if self._missing_sensors(device):
            device.state = DeviceState.DEGRADED
        else:
            device.state = DeviceState.NORMAL
        return device

    # ---- 传感器缺失/恢复（增量） ----
    def sensor_lost(self, device_id: str, sensor_name: str) -> Device:
        device = self.get(device_id)
        device.available_sensors = tuple(
            s for s in device.available_sensors if s != sensor_name
        )
        if self._missing_sensors(device):
            device.state = DeviceState.DEGRADED
        return device

    def sensor_restored(self, device_id: str, sensor_name: str,
                        at: Optional[str] = None) -> Device:
        device = self.get(device_id)
        if sensor_name not in device.available_sensors:
            device.available_sensors = (*device.available_sensors, sensor_name)
        device.last_heartbeat = at or now_iso()
        if not self._missing_sensors(device) and device.state == DeviceState.DEGRADED:
            device.state = DeviceState.NORMAL
        return device

    # ---- 在线/离线 ----
    def heartbeat_lost(self, device_id: str) -> Device:
        device = self.get(device_id)
        device.online = False
        device.state = DeviceState.OFFLINE
        return device

    def heartbeat(self, device_id: str, at: Optional[str] = None) -> Device:
        """设备重新出现：进入 recovering，必须经 verify_capabilities 重验后恢复。"""
        device = self.get(device_id)
        device.online = True
        device.last_heartbeat = at or now_iso()
        device.state = DeviceState.RECOVERING
        return device

    # ---- 执行前准入 ----
    def admit(self, device: Device, capability: Capability,
              cloud_connected: bool, channel: str) -> Optional[str]:
        """返回 None 表示可执行；否则返回拒绝原因码。

        mode 不在此处决定，由引擎结合 cloud_connected/channel 统一标注。
        """
        if device.state == DeviceState.UPDATING:
            # 固件刷新期间任何命令都不接收（含本地保底通道）。
            return REASON_DEVICE_UPDATING
        if not device.online:
            if capability.local_always and channel == "local":
                return None
            return REASON_DEVICE_OFFLINE
        if device.state == DeviceState.RECOVERING:
            # 能力尚未重验：只放行本地保底通道，其余等恢复完成。
            if capability.local_always and channel == "local":
                return None
            return REASON_DEVICE_RECOVERING
        if device.state == DeviceState.DEGRADED:
            missing = self._missing_for(device, capability)
            if missing and not capability.local_always:
                return REASON_DEVICE_DEGRADED
        return None

    def _missing_for(self, device: Device, capability: Capability) -> tuple[str, ...]:
        return tuple(
            s for s in capability.requires_sensors
            if s not in device.available_sensors
        )

    def _missing_sensors(self, device: Device) -> bool:
        """任一已声明能力所需传感器缺失即视为降级（信息更保守）。"""
        for capability in device.capabilities.values():
            if self._missing_for(device, capability):
                return True
        return False

    def to_data(self) -> list[dict]:
        return [d.to_dict() for d in self.devices.values()]

    @classmethod
    def from_data(cls, rows: list[dict]) -> "DeviceManager":
        manager = cls()
        for row in rows or []:
            device = Device.from_dict(row)
            manager.devices[device.id] = device
        return manager
