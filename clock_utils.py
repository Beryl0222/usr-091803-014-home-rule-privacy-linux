"""时间工具：全部时间为本地时区 ISO 字符串，条件按事件发生时间求值。"""

from __future__ import annotations

from datetime import datetime

ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hhmm(value: str) -> str:
    return parse_iso(value).strftime("%H:%M")


def in_time_range(value: str, start: str, end: str) -> bool:
    """闭区间判断，支持跨零点（22:00-06:00）。"""
    current = hhmm(value)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
