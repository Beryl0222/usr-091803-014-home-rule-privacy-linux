"""HTTP 契约：健康检查、事件上报、可解释查询、管理接口与鉴权。"""

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, health_payload


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from household import Household
        self.home = Household.seeded(data_dir=self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.home = self.home
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def call(self, method, path, body=None, member=None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if member:
            headers["X-Member-Id"] = member
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload
    # ---- 健康检查（保持既有契约） ----
    def test_health_contract_unchanged(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())
        self.assertEqual(payload["service"], SERVICE_ID)

    def test_unknown_route_404(self):
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)

    # ---- 身份 ----
    def test_query_requires_identity(self):
        status, payload = self.call("GET", "/api/decisions")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "identity_required")

    def test_unknown_member_rejected(self):
        status, payload = self.call("GET", "/api/decisions", member="ghost")
        self.assertEqual(status, 403)

    # ---- 事件与可解释性 ----
    def test_event_returns_redacted_explanation(self):
        status, payload = self.call("POST", "/api/events", member="mom", body={
            "type": "motion_detected",
            "device_id": "corridor_sensor",
            "occurred_at": "2026-09-18T23:05:00",
            "event_id": "h1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["winner_count"], 1)
        explanation = payload["explanation"]
        own = next(r for r in explanation["results"]
                   if r.get("rule_id") == "sleep_house_dim")
        self.assertEqual(own["command"], "off")
        other = [r for r in explanation["results"]
                 if r.get("rule_id") != "sleep_house_dim"]
        self.assertTrue(other)
        self.assertNotIn("device_id", other[0])

    def test_explain_endpoint_404_when_not_visible(self):
        _, event_payload = self.call("POST", "/api/events", member="mom", body={
            "type": "motion_detected",
            "device_id": "corridor_sensor",
            "occurred_at": "2026-09-18T23:06:00",
            "event_id": "h2",
        })
        decision_id = event_payload["decision_id"]
        status_kid, _ = self.call(
            "GET", f"/api/decisions/{decision_id}", member="kid")
        self.assertEqual(status_kid, 404)
        status_mom, payload = self.call(
            "GET", f"/api/decisions/{decision_id}", member="mom")
        self.assertEqual(status_mom, 200)
        self.assertEqual(payload["decision_id"], decision_id)

    def test_nonexistent_decision_is_404(self):
        status, _ = self.call("GET", "/api/decisions/dec-nope", member="dad")
        self.assertEqual(status, 404)

    def test_list_decisions_only_visible(self):
        self.call("POST", "/api/events", member="dad", body={
            "type": "camera_view_requested",
            "actor_id": "dad", "device_id": "living_camera",
            "occurred_at": "2026-09-18T10:05:00", "event_id": "h3",
            "payload": {"subject_id": "household"},
        })
        status, payload = self.call("GET", "/api/decisions", member="dad")
        self.assertEqual(status, 200)
        self.assertTrue(any(
            d["event_type"] == "camera_view_requested"
            for d in payload["decisions"]
        ))

    # ---- 规则版本 ----
    def test_rule_update_creates_version(self):
        status, payload = self.call(
            "POST", "/api/rules/sleep_house_dim/update", member="dad", body={
                "conditions": {"time_range": {"start": "23:00", "end": "05:00"}},
            })
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], 2)
        status, payload = self.call(
            "GET", "/api/rules/sleep_house_dim/versions", member="dad")
        self.assertEqual(status, 200)
        self.assertEqual([v["version"] for v in payload["versions"]], [1, 2])

    def test_child_cannot_modify_rules(self):
        status, payload = self.call(
            "POST", "/api/rules/elder_night_light/update",
            member="kid", body={"name": "x"})
        self.assertEqual(status, 403)

    def test_rule_versions_hidden_from_unrelated_member(self):
        status, _ = self.call(
            "GET", "/api/rules/elder_night_light/versions", member="kid")
        self.assertEqual(status, 404)

    # ---- 设备生命周期 ----
    def test_device_firmware_flow_over_api(self):
        status, payload = self.call(
            "POST", "/api/devices/bedroom_ac/firmware/begin",
            member="dad", body={"target_version": "6.0.0",
                                "at": "2026-09-19T03:00:00"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "updating")
        # 升级中事件被拒
        _, ev = self.call("POST", "/api/events", member="dad", body={
            "type": "sleep_reading", "device_id": "sleep_band",
            "occurred_at": "2026-09-19T03:01:00", "event_id": "h4",
            "payload": {"subject_id": "grandpa",
                        "readings": {"sleep_band": {"breathing": 10}}},
        })
        self.assertEqual(ev["winner_count"], 0)
        status, _ = self.call(
            "POST", "/api/devices/bedroom_ac/firmware/finished",
            member="dad", body={"target_version": "6.0.0",
                                "at": "2026-09-19T03:10:00"})
        self.assertEqual(status, 200)
        status, payload = self.call(
            "POST", "/api/devices/bedroom_ac/verify", member="dad",
            body={"available_sensors": ["temperature"],
                  "at": "2026-09-19T03:12:00"})
        self.assertEqual(payload["state"], "normal")
        self.assertEqual(payload["firmware_version"], "6.0.0")

    def test_illegal_device_transition_409(self):
        status, payload = self.call(
            "POST", "/api/devices/living_camera/firmware/finished",
            member="dad", body={"target_version": "9.0.0"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "device_transition_rejected")

    # ---- 断网 ----
    def test_cloud_toggle_requires_privilege(self):
        status, _ = self.call("POST", "/api/cloud", member="kid",
                              body={"connected": False})
        self.assertEqual(status, 403)

    def test_offline_visitor_event_still_unlocks_locally(self):
        self.call("POST", "/api/cloud", member="mom",
                  body={"connected": False})
        status, payload = self.call("POST", "/api/events", member="dad", body={
            "type": "door_access_requested",
            "actor_id": "pass-cleaner", "device_id": "front_door_lock",
            "occurred_at": "2026-09-19T11:30:00", "event_id": "h5",
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload["explanation"]["cloud_connected"] is False)
        self.assertEqual(payload["explanation"]["winners"][0]["mode"],
                         "local")
        self.call("POST", "/api/cloud", member="mom",
                  body={"connected": True})

    # ---- 同意撤回 ----
    def test_withdraw_consent_stops_new_reads(self):
        status, _ = self.call(
            "POST", "/api/consents/consent-grandpa-sleep/withdraw",
            member="grandpa")
        self.assertEqual(status, 200)
        _, payload = self.call("POST", "/api/events", member="mom", body={
            "type": "sleep_reading", "device_id": "sleep_band",
            "occurred_at": "2026-09-19T06:00:00", "event_id": "h6",
            "payload": {"subject_id": "grandpa",
                        "readings": {"sleep_band": {"breathing": 10}}},
        })
        self.assertEqual(payload["winner_count"], 0)
        self.assertIn("consent_withdrawn",
                      payload["explanation"]["results"][0]["reasons"])

    def test_cannot_withdraw_other_adults_consent(self):
        # kid 不能撤爷爷的同意（父母仅可代孩子）
        self.home.consents.grant(
            "consent-grandpa-restore", subject_id="grandpa",
            data_type=__import__("domain").DataType.HEALTH,
            purposes=("sleep_adjust",), granted_by="grandpa")
        status, _ = self.call(
            "POST", "/api/consents/consent-grandpa-restore/withdraw",
            member="kid")
        self.assertEqual(status, 403)

    # ---- 设备清单 ----
    def test_device_listing(self):
        status, payload = self.call("GET", "/api/devices", member="dad")
        self.assertEqual(status, 200)
        ids = {d["device_id"] for d in payload["devices"]}
        self.assertIn("front_door_lock", ids)
        self.assertEqual(len(ids), 6)


if __name__ == "__main__":
    unittest.main()
