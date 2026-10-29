import http.client
import json
import threading
import time
from unittest.mock import patch
from test_engine import Fixture
from uclean.server import make_server


class ServerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, route, body=None, token=True, origin=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-UClean-Token"] = self.server.session.token
        if origin:
            headers["Origin"] = self.server.origin
        if host:
            headers["Host"] = host
        conn.request("POST" if body is not None else "GET", route, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        return status, data

    def wait(self):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            state = json.loads(self.request("/api/state")[1])
            if not state["busy"]:
                return state
            time.sleep(.01)
        self.fail("job timeout")

    def test_api_requires_token(self):
        self.assertEqual(self.request("/api/state", token=False)[0], 403)

    def test_cross_origin_mutation_rejected(self):
        self.assertEqual(self.request("/api/action", {"action": "scan"}, origin=False)[0], 403)

    def test_host_rebinding_rejected(self):
        self.assertEqual(self.request("/", host="evil.example")[0], 403)

    def test_asset_path_traversal_rejected(self):
        self.assertEqual(self.request("/../key")[0], 404)

    def test_expired_session_rejected(self):
        self.server.session.started -= 5*3600
        self.assertEqual(self.request("/api/state")[0], 403)

    def test_full_button_flow_requires_preview_before_apply(self):
        cache = self.file(".npm/_cacache/data")
        self.assertEqual(self.request("/api/action", {"action": "apply", "plan_id": "a", "confirmation": "b"})[0], 409)
        self.assertEqual(self.request("/api/action", {"action": "scan", "level": "quick"})[0], 202)
        scan = self.wait()["result"]
        self.assertTrue(cache.exists())
        self.request("/api/action", {"action": "plan", "scan_id": scan["id"], "items": [scan["items"][0]["id"]]})
        plan = self.wait()["result"]
        self.assertTrue(cache.exists())
        self.assertEqual(self.request("/api/action", {"action": "apply", "plan_id":plan["id"],"confirmation":"wrong"})[0],409)
        self.request("/api/action", {"action":"apply","plan_id":plan["id"],"confirmation":plan["confirmation"]})
        receipt = self.wait()["result"]
        self.assertEqual(receipt["kind"], "receipt")
        self.assertFalse(cache.exists())

    def test_stop_before_worker_entry_is_not_lost(self):
        self.file(".npm/_cacache/data")
        scan = self.engine.scan()
        plan = self.engine.plan(scan["id"])
        self.server.session.plan = plan
        entered, release = threading.Event(), threading.Event()
        original = self.engine.apply
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)
        with patch.object(self.engine, "apply", side_effect=delayed):
            self.server.session.action({"action":"apply", "plan_id":plan["id"], "confirmation":plan["confirmation"]})
            self.assertTrue(entered.wait(1))
            self.server.session.action({"action":"cancel"})
            release.set()
            receipt = self.wait()["result"]
        self.assertEqual(receipt["items"][0]["status"], "cancelled")
        self.assertTrue((self.home/".npm/_cacache/data").exists())

    def test_restore_failure_refreshes_recovery_receipt(self):
        installer = self.file("Downloads/old.dmg")
        old = time.time()-40*86400
        import os
        os.utime(installer, (old, old))
        scan = self.engine.scan("recommended")
        plan = self.engine.plan(scan["id"], [scan["items"][0]["id"]])
        receipt = self.engine.apply(plan["id"], plan["confirmation"])
        self.server.session.result = receipt
        with patch("uclean.engine.os.link", side_effect=PermissionError()):
            self.server.session.action({"action":"restore", "receipt_id":receipt["id"], "item_id":receipt["items"][0]["id"]})
            state = self.wait()
        self.assertEqual(state["error"], "PermissionError")
        self.assertEqual(state["result"]["status"], "partial")
        self.assertTrue(state["result"]["recovery_paths"])
