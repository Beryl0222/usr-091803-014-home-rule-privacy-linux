"""家庭智能规则与隐私的运行入口。

- `python3 service.py --check`：基础配置自检；
- `python3 service.py --port 8000 --data-dir ./data`：启动本地编排服务，
  首次启动自动装配三代同堂种子场景，之后从本地恢复（断网可独立运行）。
"""

from __future__ import annotations

import argparse
import json
from http.server import ThreadingHTTPServer

from api import HomeApiHandler
from household import STATE_FILE, Household
from storage import LocalStore

SERVICE_ID = "home-rule-privacy"
SERVICE_NAME = "家庭智能规则与隐私"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(HomeApiHandler):
    """HTTP 处理器：/health 与 /api/*（实现在 api.HomeApiHandler）。"""


def build_server(port: int, data_dir: str) -> ThreadingHTTPServer:
    store = LocalStore(data_dir)
    if store.load(STATE_FILE, None) is None:
        home = Household.seeded(data_dir=data_dir)
    else:
        home = Household(data_dir=data_dir).load()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.home = home  # 供处理器读取：self.server.home
    server.data_dir = data_dir
    return server


def self_check() -> None:
    assert health_payload()["service"] == SERVICE_ID
    # 关键模块可导入、领域枚举完整
    from domain import AuthScope, Outcome, SafetyLevel  # noqa: F401
    from engine import Orchestrator  # noqa: F401
    home = Household.seeded()
    assert home.members and home.devices.devices and home.rules.history_ids()
    assert home.audit is not None
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        self_check()
        return
    server = build_server(args.port, args.data_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
