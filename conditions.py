"""规则条件求值：返回 真 / 假 / 未知 三态。

未知意味着传感器读数缺失——上层据此区分"条件不满足"与"无法安全判断"：
紧急动作可按故障安全策略放行并降级执行，普通动作必须拒绝。
"""

from __future__ import annotations

from typing import Any

from clock_utils import in_time_range

TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"


def _sensor_reading(event, spec: dict[str, Any], devices) -> tuple[str, Any]:
    device_id = spec.get("sensor_device") or event.device_id
    name = spec["name"]
    device = devices.get(device_id)
    if device is None or name not in (device.available_sensors or ()):
        return UNKNOWN, None
    readings = event.payload.get("readings", {})
    bucket = readings.get(device_id, readings)
    if name not in bucket:
        return UNKNOWN, None
    value = bucket[name]
    if "equals" in spec:
        return (TRUE if value == spec["equals"] else FALSE), value
    if "above" in spec and not (isinstance(value, (int, float)) and value > spec["above"]):
        return FALSE, value
    if "below" in spec and not (isinstance(value, (int, float)) and value < spec["below"]):
        return FALSE, value
    return TRUE, value


def evaluate(rule, event, devices, members) -> str:
    """对 rule.conditions 逐项求值；任意项为假即假，否则任一未知即未知。"""
    verdict = TRUE
    for key, spec in (rule.conditions or {}).items():
        if key == "time_range":
            if not in_time_range(event.occurred_at, spec["start"], spec["end"]):
                return FALSE
        elif key == "actor_roles":
            if event.actor_id is None:
                return FALSE
            member = members.get(event.actor_id)
            roles = spec if isinstance(spec, list) else [spec]
            if member is None or member.role not in roles:
                return FALSE
        elif key == "actor_members":
            wanted = spec if isinstance(spec, list) else [spec]
            if event.actor_id not in wanted:
                return FALSE
        elif key == "sensor":
            status, _ = _sensor_reading(event, spec, devices)
            if status == UNKNOWN:
                verdict = UNKNOWN
            elif status == FALSE:
                return FALSE
        elif key == "event_payload":
            for payload_key, expected in spec.items():
                if event.payload.get(payload_key) != expected:
                    return FALSE
        else:
            raise ValueError(f"不支持的条件类型: {key}")
    return verdict
