"""本地持久化：全部状态保存在本地 JSON，断网时服务可独立运行。

写入采用 临时文件 + 原子 rename，进程崩溃不会产生半截状态；
进程内用可重入锁保证多线程（HTTP 服务）一致。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class LocalStore:
    """按命名空间（文件名）隔离的原子 JSON 存储。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, name: str) -> threading.RLock:
        with self._locks_guard:
            if name not in self._locks:
                self._locks[name] = threading.RLock()
            return self._locks[name]

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").replace("-", "").isalnum() and not all(
            ch.isalnum() or ch in "_-" for ch in name
        ):
            raise ValueError(f"非法存储名: {name}")
        return self.data_dir / f"{name}.json"

    def load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, name: str, data: Any) -> None:
        path = self._path(name)
        with self._lock_for(name):
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.data_dir), prefix=f".{name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def with_lock(self, name: str):
        return self._lock_for(name)
