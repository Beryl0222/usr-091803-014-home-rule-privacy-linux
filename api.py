"""HTTP API：事件上报、可解释查询、规则版本、设备生命周期、同意与访客管理。

身份通过 `X-Member-Id` 头或 `as` 查询参数声明（本地家庭部署的简化模型）。
- /health 无需身份，契约保持稳定；
- 查询类接口按成员可见性投影，无权记录一律 404（不存在与不可见不可区分）；
- 管理类操作（规则修改、固件升级、凭证签发）要求 parent/elder 角色；
- 服务未装配家庭数据时 /api 返回 503，健康检查不受影响。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from clock_utils import now_iso
from domain import (
    AuthScope,
    ConsentStatus,
    DataType,
    Outcome,
    SafetyLevel,
)

PRIVILEGED_ROLES = {"parent", "elder"}
MAX_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def _json_response(handler: BaseHTTPRequestHandler, status: int,
                    payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class HomeApiHandler(BaseHTTPRequestHandler):
    server_version = "HomeRulePrivacy/1.0"

    # ---- 公共工具 ----
    @property
    def home(self):
        return getattr(self.server, "home", None)

    def _require_home(self):
        home = self.home
        if home is None:
            raise ApiError(503, "home_not_loaded", "家庭数据未加载")
        return home

    def _viewer(self, query: dict[str, list[str]]) -> str:
        viewer = self.headers.get("X-Member-Id")
        if not viewer and query.get("as"):
            viewer = query["as"][0]
        if not viewer:
            raise ApiError(401, "identity_required", "需要 X-Member-Id 身份")
        return viewer

    def _require_member(self, home, member_id: str):
        member = home.members.get(member_id)
        if member is None:
            raise ApiError(403, "unknown_member", "成员不存在")
        return member

    def _require_privileged(self, home, member_id: str):
        member = self._require_member(home, member_id)
        if member.role not in PRIVILEGED_ROLES:
            raise ApiError(403, "forbidden_role", "需要父母或长辈权限")
        return member

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "body_too_large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json", str(exc))
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
        return data

    def _handle_error(self, error: ApiError) -> None:
        _json_response(self, error.status,
                       {"error": error.code, "message": error.message})

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                from service import health_payload
                _json_response(self, 200, health_payload())
                return
            if not path.startswith("/api/"):
                _json_response(self, 404, {"error": "not_found"})
                return
            self._route_get(path, query)
        except ApiError as error:
            self._handle_error(error)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if not path.startswith("/api/"):
                _json_response(self, 404, {"error": "not_found"})
                return
            self._route_post(path, query)
        except ApiError as error:
            self._handle_error(error)

    # ---- GET ----
    def _route_get(self, path: str, query) -> None:
        if path == "/api/decisions":
            self._list_decisions(query)
        elif path.startswith("/api/decisions/"):
            self._explain_decision(path.rsplit("/", 1)[-1], query)
        elif path == "/api/rules":
            self._list_rules(query)
        elif path.startswith("/api/rules/") and path.endswith("/versions"):
            rule_id = path.split("/")[3]
            self._rule_versions(rule_id, query)
        elif path == "/api/devices":
            self._list_devices(query)
        elif path == "/api/visitor-passes":
            self._list_passes(query)
        else:
            raise ApiError(404, "not_found")

    def _list_decisions(self, query) -> None:
        home = self._require_home()
        viewer = self._viewer(query)
        self._require_member(home, viewer)
        items = []
        for decision in home.audit.list_for(viewer):
            items.append({
                "decision_id": decision.id,
                "event_id": decision.event_id,
                "event_type": decision.event_type,
                "decided_at": decision.decided_at,
                "replayed": decision.replayed,
                "executed": [
                    {"safety": r.safety.value, "scope": r.scope.value}
                    if r.requester_id != viewer
                    else {"rule_id": r.rule_id, "rule_version": r.rule_version,
                          "device_id": r.device_id, "command": r.command,
                          "safety": r.safety.value, "scope": r.scope.value,
                          "mode": r.mode}
                    for r in decision.winners
                ],
            })
        _json_response(self, 200, {"decisions": items})

    def _explain_decision(self, decision_id: str, query) -> None:
        home = self._require_home()
        viewer = self._viewer(query)
        self._require_member(home, viewer)
        projection = home.audit.explain(
            decision_id, viewer, home.rules, home.devices
        )
        if projection is None:
            raise ApiError(404, "decision_not_found")
        _json_response(self, 200, projection)

    def _list_rules(self, query) -> None:
        home = self._require_home()
        viewer = self._viewer(query)
        member = self._require_member(home, viewer)
        rules = []
        for history_id in home.rules.history_ids():
            active = home.rules.active(history_id)
            if active is None:
                continue
            if member.role not in PRIVILEGED_ROLES and active.requester_id != viewer:
                continue
            rules.append({
                "rule_id": active.rule_id,
                "version": active.version,
                "name": active.name,
                "status": active.status.value,
                "requester_id": active.requester_id,
                "safety": active.safety.value,
                "origin": active.origin,
                "updated_at": active.created_at,
            })
        _json_response(self, 200, {"rules": rules})

    def _rule_versions(self, rule_id: str, query) -> None:
        home = self._require_home()
        viewer = self._viewer(query)
        member = self._require_member(home, viewer)
        history = home.rules.history(rule_id)
        if not history:
            raise ApiError(404, "rule_not_found")
        if member.role not in PRIVILEGED_ROLES and history[0].requester_id != viewer:
            raise ApiError(404, "rule_not_found")
        _json_response(self, 200, {
            "rule_id": rule_id,
            "versions": [
                {
                    "version": r.version,
                    "name": r.name,
                    "status": r.status.value,
                    "trigger": r.trigger,
                    "source_device": r.source_device,
                    "conditions": r.conditions,
                    "device_id": r.device_id,
                    "command": r.command,
                    "requester_id": r.requester_id,
                    "safety": r.safety.value,
                    "scope": r.scope.value,
                    "origin": r.origin,
                    "purpose": r.purpose,
                    "created_at": r.created_at,
                    "parent_version": r.parent_version,
                }
                for r in history
            ],
        })

    def _list_devices(self, query) -> None:
        home = self._require_home()
        viewer = self._viewer(query)
        self._require_member(home, viewer)
        devices = [
            {
                "device_id": d.id,
                "name": d.name,
                "type": d.type.value,
                "room": d.room,
                "online": d.online,
                "state": d.state.value,
                "firmware_version": d.firmware_version,
                "previous_firmware": d.previous_firmware,
                "available_sensors": list(d.available_sensors),
                "last_heartbeat": d.last_heartbeat,
            }
            for d in home.devices.devices.values()
        ]
        _json_response(self, 200, {"devices": devices})

    def _list_passes(self, query) -> None:
        home = self._require_home()
        viewer = self._viewer(query)
        member = self._require_member(home, viewer)
        passes = []
        for visitor_pass in home.access.all():
            if member.role not in PRIVILEGED_ROLES and visitor_pass.host_id != viewer:
                continue
            passes.append({
                "id": visitor_pass.id,
                "display_name": visitor_pass.display_name,
                "device_ids": list(visitor_pass.device_ids),
                "command": visitor_pass.command,
                "valid_from": visitor_pass.valid_from,
                "valid_to": visitor_pass.valid_to,
                "host_id": visitor_pass.host_id,
                "revoked": visitor_pass.revoked,
                "use_count": visitor_pass.use_count,
            })
        _json_response(self, 200, {"passes": passes})

    # ---- POST ----
    def _route_post(self, path: str, query) -> None:
        body = self._read_body()
        viewer = self._viewer(query)
        home = self._require_home()

        # 事件可由设备/网关代访客上报，行为人的合法性由引擎按授权范围裁决
        if path == "/api/events":
            self._post_event(home, viewer, body)
            return

        self._require_member(home, viewer)
        if path == "/api/cloud":
            self._set_cloud(home, viewer, body)
        elif path == "/api/consents":
            self._grant_consent(home, viewer, body)
        elif path.startswith("/api/consents/") and path.endswith("/withdraw"):
            self._withdraw_consent(home, viewer, path.split("/")[3])
        elif path.startswith("/api/rules/") and path.endswith("/update"):
            self._update_rule(home, viewer, path.split("/")[3], body)
        elif path.startswith("/api/rules/") and path.endswith("/disable"):
            self._disable_rule(home, viewer, path.split("/")[3])
        elif path == "/api/visitor-passes":
            self._issue_pass(home, viewer, body)
        elif path.startswith("/api/visitor-passes/") and path.endswith("/revoke"):
            self._revoke_pass(home, viewer, path.split("/")[3])
        elif path.startswith("/api/devices/"):
            self._device_action(home, viewer, path.split("/")[3],
                                "/".join(path.split("/")[4:]), body)
        else:
            raise ApiError(404, "not_found")

    def _post_event(self, home, viewer: str, body) -> None:
        required = ("type",)
        for key in required:
            if not body.get(key):
                raise ApiError(400, "missing_field", f"缺少字段: {key}")
        event = home.event(
            body["type"],
            actor_id=body.get("actor_id"),
            device_id=body.get("device_id"),
            channel=body.get("channel", "local"),
            payload=body.get("payload", {}),
            occurred_at=body.get("occurred_at"),
            event_id=body.get("event_id"),
            dedup_key=body.get("dedup_key"),
        )
        decision, replayed = home.handle(event)
        projection = home.audit.explain(
            decision.id, viewer, home.rules, home.devices
        )
        _json_response(self, 200, {
            "decision_id": decision.id,
            "event_id": decision.event_id,
            "replayed": replayed,
            "winner_count": len(decision.winner_keys),
            "explanation": projection,
        })

    def _set_cloud(self, home, viewer: str, body) -> None:
        self._require_privileged(home, viewer)
        if not isinstance(body.get("connected"), bool):
            raise ApiError(400, "missing_field", "connected 必须为布尔值")
        home.set_cloud(body["connected"])
        _json_response(self, 200, {"cloud_connected": home.cloud_connected})

    def _grant_consent(self, home, viewer: str, body) -> None:
        member = self._require_member(home, viewer)
        subject_id = body.get("subject_id") or viewer
        subject = home.members.get(subject_id)
        if subject is None:
            raise ApiError(400, "unknown_subject")
        # 仅本人，或父母代表孩子；长辈自己的健康数据只能本人授权
        if subject_id != viewer:
            if member.role != "parent" or subject.role != "child":
                raise ApiError(403, "consent_guardian_only")
        purposes = body.get("purposes")
        if not isinstance(purposes, list) or not purposes:
            raise ApiError(400, "missing_field", "purposes 必须为非空数组")
        try:
            data_type = DataType(body.get("data_type", ""))
        except ValueError:
            raise ApiError(400, "invalid_data_type")
        consent = home.consents.grant(
            body.get("consent_id") or f"consent-{now_iso().replace(':','')}",
            subject_id=subject_id, data_type=data_type,
            purposes=tuple(purposes), granted_by=viewer,
            granted_at=body.get("granted_at"),
        )
        home.persist()
        _json_response(self, 201, {"consent": consent.to_dict()})

    def _withdraw_consent(self, home, viewer: str, consent_id: str) -> None:
        target = next((c for c in home.consents.all() if c.id == consent_id), None)
        if target is None:
            raise ApiError(404, "consent_not_found")
        member = self._require_member(home, viewer)
        allowed = (target.granted_by == viewer or target.subject_id == viewer
                   or (member.role == "parent"
                       and home.members.get(target.subject_id) is not None
                       and home.members[target.subject_id].role == "child"))
        if not allowed:
            raise ApiError(403, "consent_withdraw_forbidden")
        home.consents.withdraw(consent_id)
        home.persist()
        _json_response(self, 200, {"consent_id": consent_id,
                                   "status": ConsentStatus.WITHDRAWN.value})

    def _update_rule(self, home, viewer: str, rule_id: str, body) -> None:
        self._require_privileged(home, viewer)
        allowed_fields = {"name", "trigger", "source_device", "conditions",
                          "device_id", "command", "params", "safety",
                          "scope", "origin", "purpose", "data_use"}
        changes: dict[str, Any] = {}
        for key, value in body.items():
            if key not in allowed_fields:
                raise ApiError(400, "immutable_field", f"不可修改字段: {key}")
            changes[key] = value
        if "safety" in changes:
            changes["safety"] = SafetyLevel(changes["safety"])
        if "scope" in changes:
            changes["scope"] = AuthScope(changes["scope"])
        try:
            new_rule = home.rules.update(rule_id, **changes)
        except KeyError:
            raise ApiError(404, "rule_not_found")
        except ValueError as exc:
            raise ApiError(400, "rule_update_rejected", str(exc))
        home.persist()
        _json_response(self, 200, {
            "rule_id": rule_id, "version": new_rule.version,
            "status": new_rule.status.value,
        })

    def _disable_rule(self, home, viewer: str, rule_id: str) -> None:
        self._require_privileged(home, viewer)
        try:
            disabled = home.rules.disable(rule_id)
        except (KeyError, ValueError) as exc:
            raise ApiError(404, "rule_not_found", str(exc))
        home.persist()
        _json_response(self, 200, {
            "rule_id": rule_id, "version": disabled.version,
            "status": disabled.status.value,
        })

    def _issue_pass(self, home, viewer: str, body) -> None:
        self._require_privileged(home, viewer)
        try:
            visitor_pass = home.access.issue(
                pass_id=body["pass_id"],
                display_name=body["display_name"],
                device_ids=tuple(body["device_ids"]),
                command=body["command"],
                valid_from=body["valid_from"],
                valid_to=body["valid_to"],
                host_id=viewer,
            )
        except KeyError as exc:
            raise ApiError(400, "missing_field", str(exc))
        except ValueError as exc:
            raise ApiError(400, "invalid_pass", str(exc))
        home.persist()
        _json_response(self, 201, {"pass": visitor_pass.to_dict()})

    def _revoke_pass(self, home, viewer: str, pass_id: str) -> None:
        member = self._require_member(home, viewer)
        visitor_pass = next((p for p in home.access.all() if p.id == pass_id), None)
        if visitor_pass is None:
            raise ApiError(404, "pass_not_found")
        if member.role not in PRIVILEGED_ROLES and visitor_pass.host_id != viewer:
            raise ApiError(403, "pass_revoke_forbidden")
        home.access.revoke(pass_id)
        home.persist()
        _json_response(self, 200, {"pass_id": pass_id, "revoked": True})

    def _device_action(self, home, viewer: str, device_id: str,
                       action: str, body) -> None:
        self._require_privileged(home, viewer)
        manager = home.devices
        at = body.get("at")
        try:
            if action == "heartbeat":
                device = manager.heartbeat(device_id, at=at)
            elif action == "heartbeat-lost":
                device = manager.heartbeat_lost(device_id)
            elif action == "firmware/begin":
                device = manager.begin_update(
                    device_id, body["target_version"], at=at
                )
            elif action == "firmware/finished":
                device = manager.update_finished(
                    device_id, body["target_version"], at=at
                )
            elif action == "firmware/failed":
                device = manager.update_failed(device_id, at=at)
            elif action == "verify":
                device = manager.verify_capabilities(
                    device_id, tuple(body.get("available_sensors", [])), at=at
                )
            elif action == "sensors/lost":
                device = manager.sensor_lost(device_id, body["sensor"])
            elif action == "sensors/restored":
                device = manager.sensor_restored(device_id, body["sensor"], at=at)
            else:
                raise ApiError(404, "unknown_device_action")
        except KeyError as exc:
            raise ApiError(400, "missing_field", str(exc))
        except Exception as exc:  # 设备状态机非法转移
            if isinstance(exc, ApiError):
                raise
            raise ApiError(409, "device_transition_rejected", str(exc))
        home.persist()
        _json_response(self, 200, {
            "device_id": device_id, "state": device.state.value,
            "online": device.online, "firmware_version": device.firmware_version,
        })

    def log_message(self, *_args):
        return
