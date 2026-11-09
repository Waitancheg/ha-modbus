"""Ingress web UI and JSON API."""

import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app import __version__
from app.bus import BAUDS, Bus, list_serial_ports
from app.ha_api import HaError, HubControl
from app.profiles import decode_read, load_profiles, profile_by_id, public_profile
from app.rtu import (
    READ_FC,
    append_crc,
    build_broadcast_address,
    build_read,
    build_write_coil,
    build_write_coils,
    build_write_register,
    build_write_registers,
    crc_ok,
    hex_bytes,
    parse_hex,
    probe_device,
)
from app.serial_port import SerialError
from app.yaml_export import generate_yaml

STATIC = Path(__file__).resolve().parent / "static"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_options() -> dict:
    path = os.environ.get("OPTIONS_PATH", "/data/options.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _clean_settings(raw: dict, base: dict | None = None) -> dict:
    src = dict(base or {})
    src.update(raw or {})
    baud = int(src.get("baudrate") or 9600)
    if baud not in BAUDS:
        raise ValueError("скорость не из списка")
    parity = str(src.get("parity") or "N").upper()
    if parity not in ("N", "E", "O"):
        raise ValueError("чётность: N, E или O")
    stopbits = int(src.get("stopbits") or 1)
    if stopbits not in (1, 2):
        raise ValueError("стоп-биты: 1 или 2")
    device = str(src.get("device") or "").strip()
    if not device.startswith("/dev/"):
        raise ValueError("устройство должно быть путём /dev/…")
    timeout = float(src.get("timeout") if src.get("timeout") is not None else 0.25)
    gap = float(src.get("inter_frame_delay") or 0)
    echo = float(src.get("echo_guard") if src.get("echo_guard") is not None else 0.005)
    if not 0.05 <= timeout <= 10:
        raise ValueError("таймаут 0.05…10 с")
    if not 0 <= gap <= 2:
        raise ValueError("пауза между кадрами 0…2 с")
    if not 0 <= echo <= 0.2:
        raise ValueError("порог эха 0…0.2 с")
    return {
        "device": device,
        "baudrate": baud,
        "parity": parity,
        "stopbits": stopbits,
        "timeout": timeout,
        "inter_frame_delay": gap,
        "echo_guard": echo,
        "hub_name": str(src.get("hub_name") or "").strip(),
        "auto_release_hub": bool(src.get("auto_release_hub", True)),
    }


class App:
    def __init__(self):
        self.settings = _clean_settings(load_options(), {
            "device": "/dev/ttyUSB0",
            "baudrate": 9600,
            "parity": "N",
            "stopbits": 1,
            "timeout": 0.25,
            "inter_frame_delay": 0,
            "echo_guard": 0.005,
            "hub_name": "",
            "auto_release_hub": True,
        })
        self.bus = Bus()
        self.hub = HubControl()
        self.profiles = load_profiles()
        self.pending = {}
        self._scan_lock = threading.Lock()
        self.scan = self._empty_scan()

    def _empty_scan(self) -> dict:
        return {
            "running": False,
            "cancel": threading.Event(),
            "done": 0,
            "total": 0,
            "current": None,
            "found": [],
            "error": None,
            "finished": False,
        }

    def reload_profiles(self) -> None:
        self.profiles = load_profiles()

    def profile(self, profile_id: str) -> dict:
        found = profile_by_id(self.profiles, profile_id)
        if not found:
            raise ValueError("профиль не найден")
        return found

    def scan_view(self) -> dict:
        with self._scan_lock:
            return {
                "running": self.scan["running"],
                "done": self.scan["done"],
                "total": self.scan["total"],
                "current": self.scan["current"],
                "found": list(self.scan["found"]),
                "error": self.scan["error"],
                "finished": self.scan["finished"],
            }

    def _hub_name(self) -> str:
        return (self.settings.get("hub_name") or "").strip()

    def release_hub(self) -> dict:
        name = self._hub_name()
        if not name:
            raise HaError("укажите имя хаба Modbus")
        _log(f"modbus.stop hub={name}")
        return self.hub.stop(name)

    def restore_hub(self) -> dict:
        name = self._hub_name()
        if not name:
            raise HaError("укажите имя хаба Modbus")
        _log(f"restore hub={name}")
        return self.hub.restart(name)

    def _with_hub(self, action):
        name = self._hub_name()
        auto = bool(self.settings.get("auto_release_hub")) and bool(name) and not self.hub.released
        if auto:
            self.release_hub()
        try:
            return action()
        finally:
            if auto:
                try:
                    self.restore_hub()
                except HaError as exc:
                    raise HaError(f"операция на шине завершилась, хаб не восстановлен: {exc}") from exc

    def _busy(self) -> None:
        if self.scan["running"]:
            raise ValueError("дождитесь окончания сканирования")

    def _remember(self, frame: bytes, summary: str) -> dict:
        now = time.time()
        self.pending = {key: item for key, item in self.pending.items() if item["exp"] > now}
        token = secrets.token_urlsafe(12)
        self.pending[token] = {"frame": frame, "summary": summary, "exp": now + 120}
        return {"token": token, "summary": summary, "hex": hex_bytes(frame), "expires": 120}

    def _check_forbid(self, profile: dict | None, address: int, values: list) -> None:
        if not profile:
            return
        blocked = set()
        text = "запрещённое значение"
        for point in profile.get("points") or []:
            if int(point.get("address", -1)) != int(address):
                continue
            for item in point.get("forbid") or []:
                blocked.add(int(item))
            if point.get("forbid_text"):
                text = point["forbid_text"]
        for value in values:
            if int(value) in blocked:
                raise ValueError(text)

    def build_write(self, body: dict) -> dict:
        function = int(body.get("fc"))
        slave = int(body.get("slave"))
        address = int(body.get("address") or 0)
        if not 0 <= slave <= 255:
            raise ValueError("адрес устройства 0..255")
        if not 0 <= address <= 65535:
            raise ValueError("адрес регистра 0..65535")
        profile = self.profile(body["profile_id"]) if body.get("profile_id") else None
        if function == 5:
            frame = build_write_coil(slave, address, bool(body.get("on")))
            state = "ВКЛ (0xFF00)" if body.get("on") else "ВЫКЛ (0x0000)"
            summary = f"FC05, устройство {slave}, катушка {address}: {state}"
        elif function == 6:
            value = int(body["value"]) & 0xFFFF
            self._check_forbid(profile, address, [value])
            frame = build_write_register(slave, address, value)
            summary = f"FC06, устройство {slave}, регистр {address} = {value}"
        elif function == 15:
            coils = [1 if int(item) else 0 for item in body.get("coils") or []]
            frame = build_write_coils(slave, address, coils)
            summary = f"FC15, устройство {slave}, катушки с {address}: {coils}"
        elif function == 16:
            values = [int(item) & 0xFFFF for item in body.get("values") or []]
            self._check_forbid(profile, address, values)
            frame = build_write_registers(slave, address, values)
            summary = f"FC16, устройство {slave}, регистры с {address}: {values}"
        else:
            raise ValueError("функция записи: 5, 6, 15 или 16")
        return self._remember(frame, summary)

    def commit_write(self, token: str) -> dict:
        self._busy()
        item = self.pending.pop(token, None)
        if not item or item["exp"] < time.time():
            raise ValueError("подтверждение устарело, подготовьте кадр ещё раз")
        frame = item["frame"]

        def run():
            result = self.bus.transact(frame, self.settings, keep_open=False)
            result["tx"] = hex_bytes(frame)
            result["summary"] = item["summary"]
            return _public_exchange(result)

        return self._with_hub(run)

    def read(self, body: dict) -> dict:
        self._busy()
        function = int(body.get("fc"))
        if function not in READ_FC:
            raise ValueError("функция чтения: 1, 2, 3 или 4")
        slave = int(body.get("slave"))
        address = int(body.get("address") or 0)
        quantity = int(body.get("quantity") or 1)
        if not 0 <= slave <= 255 or not 0 <= address <= 65535:
            raise ValueError("адрес вне диапазона")
        limit = 125 if function in (3, 4) else 2000
        if not 1 <= quantity <= limit:
            raise ValueError(f"количество 1..{limit}")
        frame = build_read(slave, function, address, quantity)
        profile = self.profile(body["profile_id"]) if body.get("profile_id") else None

        def run():
            result = self.bus.transact(frame, self.settings, keep_open=False)
            result["tx"] = hex_bytes(frame)
            public = _public_exchange(result)
            public["decoded"] = decode_read(profile, function, address, result.get("parsed") or {})
            return public

        return self._with_hub(run)

    def raw(self, body: dict) -> dict:
        self._busy()
        payload = parse_hex(body.get("hex") or "")
        frame = append_crc(payload) if body.get("append_crc", True) else payload
        if body.get("append_crc", True) is False and len(frame) >= 4 and not crc_ok(frame):
            pass
        note = "CRC16 добавлен" if body.get("append_crc", True) else "CRC не добавлялся"

        def run():
            result = self.bus.transact(frame, self.settings, keep_open=False)
            result["tx"] = hex_bytes(frame)
            public = _public_exchange(result)
            public["note"] = note if not public.get("note") else f"{note}. {public['note']}"
            return public

        return self._with_hub(run)

    def profile_run(self, body: dict) -> dict:
        profile = self.profile(body.get("profile_id"))
        action_id = body.get("action")
        action = None
        for item in profile.get("actions") or []:
            if item.get("id") == action_id:
                action = item
                break
        if not action:
            raise ValueError("действие не найдено")
        op = action.get("op")
        if op == "read":
            return self.read(
                {
                    "slave": body.get("slave"),
                    "fc": action["fc"],
                    "address": action.get("address") or 0,
                    "quantity": action.get("quantity") or 1,
                    "profile_id": profile["id"],
                }
            )
        if op == "raw":
            return self.raw({"hex": action["hex"], "append_crc": action.get("append_crc", True)})
        if op == "broadcast_address":
            frame = build_broadcast_address(int(body.get("value")))
            summary = f"Broadcast FC16, регистр 0, новый адрес {int(body.get('value'))}"
            return self._remember(frame, summary)
        if op == "write":
            payload = {
                "fc": int(action["fc"]),
                "slave": body.get("slave"),
                "address": action.get("address") or 0,
                "profile_id": profile["id"],
            }
            if int(action["fc"]) == 5:
                payload["on"] = bool(body.get("on"))
            elif int(action["fc"]) == 16:
                payload["values"] = [int(body.get("value"))]
            else:
                payload["value"] = int(body.get("value"))
            forbid = [int(item) for item in action.get("forbid") or []]
            if int(body.get("value") or 0) in forbid:
                raise ValueError(action.get("forbid_text") or "запрещённое значение")
            return self.build_write(payload)
        raise ValueError("неизвестное действие")

    def start_scan(self, body: dict) -> dict:
        start = int(body.get("address_from"))
        end = int(body.get("address_to"))
        if not 1 <= start <= 255 or not 1 <= end <= 255 or end < start:
            raise ValueError("диапазон адресов 1..255, включая 255")
        bauds = [int(item) for item in body.get("bauds") or []]
        if not bauds or any(item not in BAUDS for item in bauds):
            raise ValueError("выберите скорость из списка")
        addresses = list(range(start, end + 1))
        with self._scan_lock:
            if self.scan["running"]:
                raise ValueError("сканирование уже идёт")
            cancel = threading.Event()
            self.scan = {
                "running": True,
                "cancel": cancel,
                "done": 0,
                "total": len(addresses) * len(bauds),
                "current": None,
                "found": [],
                "error": None,
                "finished": False,
            }

        def work():
            def run():
                try:
                    for baud in bauds:
                        settings = dict(self.settings)
                        settings["baudrate"] = baud
                        for address in addresses:
                            if cancel.is_set():
                                with self._scan_lock:
                                    self.scan["error"] = "остановлено"
                                return
                            with self._scan_lock:
                                self.scan["current"] = {"address": address, "baudrate": baud}

                            def transact(frame, settings=settings):
                                if frame[1] not in READ_FC:
                                    raise RuntimeError("сканирование пыталось записать на шину")
                                return self.bus.transact(frame, settings, keep_open=True)

                            found = probe_device(transact, address)
                            with self._scan_lock:
                                self.scan["done"] += 1
                                if found["present"]:
                                    found["baudrate"] = baud
                                    self.scan["found"].append(found)
                finally:
                    self.bus.close()

            try:
                self._with_hub(run)
            except Exception as exc:
                _log(f"scan error: {exc}")
                with self._scan_lock:
                    self.scan["error"] = str(exc)
            finally:
                with self._scan_lock:
                    self.scan["running"] = False
                    self.scan["finished"] = True
                    self.scan["current"] = None

        threading.Thread(target=work, name="modbus-scan", daemon=True).start()
        return self.scan_view()

    def stop_scan(self) -> dict:
        self.scan["cancel"].set()
        return self.scan_view()


def _public_exchange(result: dict) -> dict:
    parsed = result.get("parsed")
    note = ""
    if result.get("timeout") and not result.get("raw"):
        note = "нет ответа"
    elif result.get("echo") and not result.get("frame"):
        note = "только локальное эхо, ответа устройства нет"
    elif result.get("raw") and not result.get("crc_ok"):
        note = "ответ без корректного CRC (для broadcast это обычно)"
    return {
        "ok": True,
        "tx": result.get("tx") or "",
        "rx": result.get("raw_hex") or "",
        "echo": bool(result.get("echo")),
        "timeout": bool(result.get("timeout")),
        "crc_ok": bool(result.get("crc_ok")),
        "parsed": parsed,
        "note": note,
        "summary": result.get("summary") or "",
        "decoded": [],
    }


APP = App()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _allowed(self) -> bool:
        if os.environ.get("MODBUS_TOOL_OPEN") == "1":
            return True
        host = self.client_address[0]
        return host.startswith("172.30.32.") or host in ("127.0.0.1", "::1")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json(404, {"ok": False, "error": "не найдено"})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ValueError("слишком большой запрос")
        if length <= 0:
            return {}
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ожидался объект JSON")
        return data

    def _guard(self, action) -> None:
        if not self._allowed():
            self._json(403, {"ok": False, "error": "доступ только через панель Home Assistant"})
            return
        try:
            action()
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "некорректный JSON"})
        except (ValueError, SerialError, HaError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            _log(f"error: {exc}")
            self._json(500, {"ok": False, "error": str(exc)})

    def do_GET(self):
        def run():
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._file(STATIC / "index.html")
                return
            if path.startswith("/static/"):
                name = Path(path).name
                self._file(STATIC / name)
                return
            if path == "/health":
                self._json(200, {"ok": True, "version": __version__})
                return
            if path == "/api/state":
                self._json(200, {
                    "ok": True,
                    "version": __version__,
                    "settings": APP.settings,
                    "gap": APP.bus.effective_gap(APP.settings),
                    "ports": list_serial_ports(),
                    "bauds": list(BAUDS),
                    "hub": {
                        "released": APP.hub.released,
                        "last": APP.hub.last,
                        "supervisor": bool(os.environ.get("SUPERVISOR_TOKEN")),
                    },
                    "profiles": [
                        {
                            "id": item["id"],
                            "name": item.get("name"),
                            "subtitle": item.get("subtitle"),
                        }
                        for item in APP.profiles
                    ],
                    "scan": APP.scan_view(),
                })
                return
            if path == "/api/profiles":
                self._json(200, {"ok": True, "profiles": [public_profile(item) for item in APP.profiles]})
                return
            if path == "/api/scan":
                self._json(200, {"ok": True, "scan": APP.scan_view()})
                return
            if path == "/api/log":
                self._json(200, {"ok": True, "log": APP.bus.snapshot_log()})
                return
            self._json(404, {"ok": False, "error": "не найдено"})

        self._guard(run)

    def do_POST(self):
        def run():
            path = urlparse(self.path).path
            body = self._read_json()
            if path == "/api/settings":
                APP.settings = _clean_settings(body, APP.settings)
                self._json(200, {"ok": True, "settings": APP.settings, "gap": APP.bus.effective_gap(APP.settings)})
                return
            if path == "/api/hub/release":
                self._json(200, {"ok": True, "result": APP.release_hub()})
                return
            if path == "/api/hub/restore":
                self._json(200, {"ok": True, "result": APP.restore_hub()})
                return
            if path == "/api/scan/start":
                self._json(200, {"ok": True, "scan": APP.start_scan(body)})
                return
            if path == "/api/scan/stop":
                self._json(200, {"ok": True, "scan": APP.stop_scan()})
                return
            if path == "/api/read":
                self._json(200, APP.read(body))
                return
            if path == "/api/raw":
                self._json(200, APP.raw(body))
                return
            if path == "/api/write/preview":
                self._json(200, {"ok": True, "preview": APP.build_write(body)})
                return
            if path == "/api/write/commit":
                self._json(200, APP.commit_write(str(body.get("token") or "")))
                return
            if path == "/api/profile/run":
                result = APP.profile_run(body)
                if "token" in result:
                    self._json(200, {"ok": True, "preview": result})
                else:
                    self._json(200, result)
                return
            if path == "/api/yaml":
                profile = APP.profile(body.get("profile_id"))
                settings = {
                    "hub": body.get("hub") or APP.settings.get("hub_name") or "modbus_hub",
                    "device": APP.settings["device"],
                    "baudrate": body.get("baudrate") or APP.settings["baudrate"],
                    "parity": APP.settings["parity"],
                    "stopbits": APP.settings["stopbits"],
                    "address": int(body.get("address")),
                }
                self._json(200, {"ok": True, "yaml": generate_yaml(profile, settings)})
                return
            self._json(404, {"ok": False, "error": "не найдено"})

        self._guard(run)


def main() -> None:
    host = os.environ.get("MODBUS_TOOL_HOST", "0.0.0.0")
    port = int(os.environ.get("MODBUS_TOOL_PORT", "8099"))
    _log(f"Modbus RTU Tool {__version__} on {host}:{port}")
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.bus.close()
        server.server_close()


if __name__ == "__main__":
    main()
