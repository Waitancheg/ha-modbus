import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rtu import build_write_register, hex_bytes
from app.server import APP, Handler


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_profiles_and_page(self):
        status, health = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["version"], "0.1.0")
        status, page = 200, urllib.request.urlopen(f"http://127.0.0.1:{self.port}/").read().decode()
        self.assertIn("Сканирование", page)
        status, profiles = self._call("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(len(profiles["profiles"]), 3)

    def test_profile_blocks_factory_reset_and_previews_baud(self):
        status, denied = self._call("POST", "/api/profile/run", {
            "profile_id": "eletechsup_n4dsa02",
            "action": "set_baud",
            "slave": 1,
            "value": 5,
        })
        self.assertEqual(status, 400)
        self.assertIn("сброс", denied["error"])
        status, ok = self._call("POST", "/api/profile/run", {
            "profile_id": "eletechsup_n4dsa02",
            "action": "set_baud",
            "slave": 1,
            "value": 3,
        })
        self.assertEqual(status, 200)
        self.assertEqual(ok["preview"]["hex"], hex_bytes(build_write_register(1, 3, 3)))

    def test_broadcast_preview_and_yaml(self):
        status, preview = self._call("POST", "/api/profile/run", {
            "profile_id": "chinese_relay_2_4ch",
            "action": "set_address",
            "slave": 255,
            "value": 20,
        })
        self.assertEqual(status, 200)
        self.assertTrue(preview["preview"]["hex"].startswith("00 10 00 00 00 01 02 00 14"))
        status, yaml_body = self._call("POST", "/api/yaml", {
            "profile_id": "r4dcb08",
            "address": 7,
            "hub": "modbus_hub",
        })
        self.assertEqual(status, 200)
        self.assertEqual(yaml_body["yaml"].count("device_address: 7"), 8)
        self.assertIn("device_class: temperature", yaml_body["yaml"])

    def test_missing_port_is_an_error_not_a_write(self):
        previous = APP.settings["device"]
        APP.settings["device"] = "/dev/ttyUSB-modbus-tool-missing"
        try:
            status, body = self._call("POST", "/api/read", {"slave": 1, "fc": 3, "address": 0, "quantity": 1})
        finally:
            APP.settings["device"] = previous
        self.assertEqual(status, 400)
        self.assertIn("не найдено", body["error"])


if __name__ == "__main__":
    unittest.main()
