"""本地持久化：所有关键状态保存在家庭本地，断网仍可工作。

使用单个 JSON 文件 + 临时文件原子替换（os.replace），避免写到一半损坏。
任何云端规则都不会成为唯一事实来源——门锁在断网时依旧依据本地状态受控。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Optional

SCHEMA_VERSION = 1


class LocalStore:
    """线程安全的本地键值/集合存储。

    逻辑上分为若干集合（members / devices / consents / rules / rule_versions /
    visitor_passes / audit / processed_events）。写操作立刻落盘。
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._empty()
        if path and os.path.exists(path):
            self._load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "members": {},
            "devices": {},
            "consents": [],
            "rules": {},
            "rule_versions": [],
            "visitor_passes": {},
            "audit": [],
            "processed_events": {},
            "pending_events": [],
        }

    # ---- 基础读写 -------------------------------------------------------

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _load(self) -> None:
        with open(self._path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 向前兼容：补齐缺失集合
        merged = self._empty()
        merged.update(data)
        self._data = merged

    def _flush(self) -> None:
        if not self._path:
            return
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".home-state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def snapshot(self) -> dict[str, Any]:
        """返回深拷贝，便于在不持锁的情况下读取一致视图。"""
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    # ---- 通用集合操作 ---------------------------------------------------

    def put(self, collection: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[collection][key] = value
            self._flush()

    def get(self, collection: str, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._data[collection].get(key)
            return json.loads(json.dumps(item)) if item is not None else None

    def all(self, collection: str) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(v)) for v in self._data[collection].values()]

    def delete(self, collection: str, key: str) -> None:
        with self._lock:
            self._data[collection].pop(key, None)
            self._flush()

    def append(self, collection: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[collection].append(value)
            self._flush()

    def append_many(self, collection: str, values: list[dict[str, Any]]) -> None:
        if not values:
            return
        with self._lock:
            self._data[collection].extend(values)
            self._flush()

    def replace_list(self, collection: str, values: list[dict[str, Any]]) -> None:
        with self._lock:
            self._data[collection] = values
            self._flush()

    def list_of(self, collection: str) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(v)) for v in self._data[collection]]

    def put_map(self, collection: str, key: str, value: Any) -> None:
        """processed_events 这类非 dict 值的写入。"""
        with self._lock:
            self._data[collection][key] = value
            self._flush()

    def contains(self, collection: str, key: str) -> bool:
        with self._lock:
            return key in self._data[collection]
