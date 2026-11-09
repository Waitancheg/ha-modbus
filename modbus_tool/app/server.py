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
    PROBE_PLAN,
    READ_FC,
    append_crc,
    build_broadcast_address,
    build_read,
    build_write_coil,
    build_write_coils,
    build_write_register,
    build_write_registers,
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


def _read_json_file(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_options() -> dict:
    return _read_json_file(os.environ.get("OPTIONS_PATH", "/data/options.json"))


def settings_path() -> str:
    return os.environ.get("SETTINGS_PATH", "/data/settings.json")


def load_saved() -> dict:
    return _read_json_file(settings_path())


def save_settings(settings: dict) -> None:
    path = settings_path()
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except OSError as exc:
        _log(f"не удалось сохранить настройки: {exc}")


def _clean_scan(raw: dict | None) -> dict:
    src = raw or {}
    start = int(src.get("address_from") or 1)
    end = int(src.get("address_to") or 255)
    start = min(255, max(1, start))
    end = min(255, max(start, end))
    bauds = []
    for item in src.get("bauds") or [9600]:
        baud = int(item)
        if baud not in BAUDS:
            raise ValueError("скорость скана не из списка")
        if baud not in bauds:
            bauds.append(baud)
    parities = []
    for item in src.get("parities") or ["N"]:
        parity = str(item).upper()
        if parity not in ("N", "E", "O"):
            raise ValueError("чётность скана: N, E или O")
        if parity not in parities:
            parities.append(parity)
    probes = []
    for item in src.get("probes") or [3, 1, 2]:
        function = int(item)
        if function not in (1, 2, 3):
            raise ValueError("опрос: FC01, FC02 или FC03")
        if function not in probes:
            probes.append(function)
    if not probes:
        raise ValueError("выберите хотя бы одну функцию опроса")
    return {
        "address_from": start,
        "address_to": end,
        "bauds": bauds,
        "parities": parities,
        "probes": probes,
    }


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
    bytesize = int(src.get("bytesize") or 8)
    if bytesize not in (5, 6, 7, 8):
        raise ValueError("биты данных: 5, 6, 7 или 8")
    device = str(src.get("device") or "").strip()
    if not device.startswith("/dev/"):
        raise ValueError("устройство должно быть путём /dev/…")
    timeout = float(src.get("timeout") if src.get("timeout") is not None else 0.25)
    gap = float(src.get("inter_frame_delay") or 0)
    echo = float(src.get("echo_guard") if src.get("echo_guard") is not None else 0.005)
    retries = int(src.get("retries") or 0)
    if not 0.05 <= timeout <= 10:
        raise ValueError("таймаут 0.05…10 с")
    if not 0 <= gap <= 2:
        raise ValueError("пауза между кадрами 0…2 с")
    if not 0 <= echo <= 0.2:
        raise ValueError("порог эха 0…0.2 с")
    if not 0 <= retries <= 5:
        raise ValueError("повторы 0…5")
    scan_src = src.get("scan") if isinstance(src.get("scan"), dict) else {}
    if raw and isinstance(raw.get("scan"), dict):
        scan_src = raw["scan"]
    return {
        "device": device,
        "baudrate": baud,
        "parity": parity,
        "stopbits": stopbits,
        "bytesize": bytesize,
        "timeout": timeout,
        "inter_frame_delay": gap,
        "echo_guard": echo,
        "retries": retries,
        "hub_name": str(src.get("hub_name") or "").strip(),
        "ha_not_using_port": bool(src.get("ha_not_using_port", False)),
        "scan": _clean_scan(scan_src),
    }


class App:
    def __init__(self):
        defaults = {
            "device": "/dev/ttyUSB0",
            "baudrate": 9600,
            "parity": "N",
            "stopbits": 1,
            "bytesize": 8,
            "timeout": 0.25,
            "inter_frame_delay": 0,
            "echo_guard": 0.005,
            "retries": 0,
            "hub_name": "",
            "ha_not_using_port": False,
        }
        # Addon options are the first-run defaults. The panel file wins after that.
        initial = dict(load_options())
        initial.update(load_saved())
        self.settings = _clean_settings(initial, defaults)
        self.bus = Bus()
        self.hub = HubControl()
        self.profiles = load_profiles()
        self.pending = {}
        self._scan_lock = threading.Lock()
        self._hub_lock = threading.Lock()
        self._inflight = 0
        self._auto_held = False
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

    def _ensure_port_policy(self) -> None:
        if self._hub_name() or self.settings.get("ha_not_using_port"):
            return
        raise HaError("Укажите имя хаба или отметьте «HA не использует этот порт»")

    def port_view(self) -> dict:
        path = self.settings["device"]
        return {"path": path, "exists": os.path.exists(path)}

    def hub_view(self) -> dict:
        name = self._hub_name()
        if self.hub.manual and name:
            mode, label = "session", f"сессия: хаб «{name}» остановлен"
        elif name:
            mode, label = "auto", f"авто: «{name}» — stop перед операцией, reload после"
        elif self.settings.get("ha_not_using_port"):
            mode, label = "unused", "HA не использует этот порт"
        else:
            mode, label = "blocked", "нет хаба — операции запрещены"
        return {
            "name": name,
            "mode": mode,
            "label": label,
            "manual": self.hub.manual,
            "supervisor": bool(os.environ.get("SUPERVISOR_TOKEN")),
            "last": self.hub.last,
        }

    def apply_settings(self, raw: dict) -> dict:
        self.settings = _clean_settings(raw, self.settings)
        save_settings(self.settings)
        return self.settings

    def release_hub(self) -> dict:
        name = self._hub_name()
        if not name:
            raise HaError("укажите имя хаба Modbus")
        with self._hub_lock:
            _log(f"modbus.stop hub={name}")
            result = self.hub.stop(name)
            self.hub.manual = True
            self._auto_held = False
            return result

    def restore_hub(self) -> dict:
        name = self._hub_name()
        if not name:
            raise HaError("укажите имя хаба Modbus")
        with self._hub_lock:
            _log(f"modbus.reload hub={name}")
            self.hub.manual = False
            self._auto_held = False
            return self.hub.reload()

    def _with_hub(self, action):
        self._ensure_port_policy()
        name = self._hub_name()
        with self._hub_lock:
            if name and not self.hub.manual and self._inflight == 0:
                _log(f"modbus.stop hub={name}")
                self.hub.stop(name)
                self._auto_held = True
            self._inflight += 1
        try:
            return action()
        finally:
            with self._hub_lock:
                self._inflight -= 1
                if self._inflight == 0 and self._auto_held and not self.hub.manual:
                    self._auto_held = False
                    try:
                        _log("modbus.reload")
                        self.hub.reload()
                    except HaError as exc:
                        raise HaError(f"операция на шине завершилась, хаб не восстановлен: {exc}") from exc
                elif self._inflight == 0 and self.hub.manual:
                    self._auto_held = False

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

    def _exchange(self, frame: bytes, summary: str = "", note: str = "") -> dict:
        result = self.bus.transact(frame, self.settings, keep_open=False)
        result["tx"] = hex_bytes(frame)
        result["summary"] = summary
        public = _public_exchange(result)
        if note:
            public["note"] = note if not public.get("note") else f"{note}. {public['note']}"
        return public

    def commit_write(self, token: str) -> dict:
        self._busy()
        item = self.pending.pop(token, None)
        if not item or item["exp"] < time.time():
            raise ValueError("подтверждение устарело, подготовьте кадр ещё раз")
        frame = item["frame"]
        return self._with_hub(lambda: self._exchange(frame, item["summary"], item.get("note") or ""))

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
        payload = parse_hex(body.get("hex") or "")
        appended = bool(body.get("append_crc", True))
        frame = append_crc(payload) if appended else payload
        if len(frame) < 2:
            raise ValueError("кадр короче 2 байт")
        slave, function = frame[0], frame[1]
        note = "CRC16 добавлен" if appended else "CRC не добавлялся"
        if slave == 0 or function not in READ_FC:
            summary = f"HEX, устройство {slave}, функция {function:02X}"
            if slave == 0:
                summary = "Broadcast. " + summary
            remembered = self._remember(frame, summary)
            remembered["note"] = note
            return remembered
        self._busy()

        def run():
            return self._exchange(frame, note=note)

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

    def start_scan(self, body: dict | None = None) -> dict:
        self._ensure_port_policy()
        scan = _clean_scan(body or self.settings.get("scan"))
        self.settings["scan"] = scan
        save_settings(self.settings)
        addresses = list(range(scan["address_from"], scan["address_to"] + 1))
        bauds = scan["bauds"]
        parities = scan["parities"]
        probes = [item for item in PROBE_PLAN if item[0] in scan["probes"]]
        with self._scan_lock:
            if self.scan["running"]:
                raise ValueError("сканирование уже идёт")
            cancel = threading.Event()
            self.scan = {
                "running": True,
                "cancel": cancel,
                "done": 0,
                "total": len(addresses) * len(bauds) * len(parities),
                "current": None,
                "found": [],
                "error": None,
                "finished": False,
            }

        def work():
            def run():
                try:
                    for baud in bauds:
                        for parity in parities:
                            tuned = dict(self.settings)
                            tuned["baudrate"] = baud
                            tuned["parity"] = parity
                            for address in addresses:
                                if cancel.is_set():
                                    with self._scan_lock:
                                        self.scan["error"] = "остановлено"
                                    return
                                with self._scan_lock:
                                    self.scan["current"] = {
                                        "address": address,
                                        "baudrate": baud,
                                        "parity": parity,
                                    }

                                def transact(frame, tuned=tuned):
                                    if frame[1] not in READ_FC:
                                        raise RuntimeError("сканирование пыталось записать на шину")
                                    return self.bus.transact(frame, tuned, keep_open=True)

                                found = probe_device(transact, address, probes)
                                with self._scan_lock:
                                    self.scan["done"] += 1
                                    if found["present"]:
                                        found["baudrate"] = baud
                                        found["parity"] = parity
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
    if result.get("broadcast"):
        note = "отправлено (broadcast, ответ не ожидается)"
        if result.get("raw"):
            note += ". Принятые байты показаны ниже"
    elif result.get("timeout") and not result.get("raw"):
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
                    "port": APP.port_view(),
                    "hub": APP.hub_view(),
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
                APP.apply_settings(body)
                self._json(200, {
                    "ok": True,
                    "settings": APP.settings,
                    "gap": APP.bus.effective_gap(APP.settings),
                    "port": APP.port_view(),
                    "hub": APP.hub_view(),
                })
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
                result = APP.raw(body)
                if "token" in result:
                    self._json(200, {"ok": True, "preview": result})
                else:
                    self._json(200, result)
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
                    "bytesize": APP.settings.get("bytesize") or 8,
                    "address": int(body.get("address")),
                    "full_hub": bool(body.get("full_hub")),
                    "channels": body.get("channels"),
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
