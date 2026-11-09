"""Serialized access to one RS485 port."""

import glob
import os
import threading
import time
from collections import deque

from app.rtu import WRITE_FC, auto_gap, extract_response, hex_bytes
from app.serial_port import SerialError, SerialPort

BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)


def list_serial_ports() -> list:
    found = []
    seen = set()
    patterns = (
        "/dev/serial/by-id/*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/ttyAMA*",
        "/dev/ttyS*",
    )
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            if path in seen or not os.path.exists(path):
                continue
            seen.add(path)
            found.append({"path": path, "real": os.path.realpath(path)})
    return found


class Bus:
    def __init__(self):
        self._lock = threading.RLock()
        self._port = SerialPort()
        self._settings = None
        self.log = deque(maxlen=400)
        self._log_seq = 0

    def _note(self, direction: str, data: bytes, note: str = "") -> None:
        self._log_seq += 1
        self.log.append(
            {
                "id": self._log_seq,
                "ts": time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
                "dir": direction,
                "hex": hex_bytes(data),
                "note": note,
            }
        )

    def snapshot_log(self, after: int = 0) -> list:
        with self._lock:
            return [item for item in self.log if item["id"] > after]

    def close(self) -> None:
        with self._lock:
            self._port.close()
            self._settings = None

    def _ensure(self, settings: dict) -> None:
        key = (
            settings["device"],
            int(settings["baudrate"]),
            settings["parity"],
            int(settings["stopbits"]),
            int(settings.get("bytesize") or 8),
        )
        if self._port.open and self._settings == key:
            return
        self._port.configure(*key)
        self._settings = key

    def effective_gap(self, settings: dict) -> float:
        gap = float(settings.get("inter_frame_delay") or 0)
        if gap > 0:
            return gap
        return auto_gap(
            int(settings["baudrate"]),
            settings["parity"],
            int(settings["stopbits"]),
            int(settings.get("bytesize") or 8),
        )

    def transact(self, frame: bytes, settings: dict, keep_open: bool = False) -> dict:
        gap = self.effective_gap(settings)
        echo_guard = float(settings.get("echo_guard") if settings.get("echo_guard") is not None else 0.005)
        broadcast_write = len(frame) >= 2 and frame[0] == 0 and frame[1] in WRITE_FC
        timeout = float(settings["timeout"])
        if broadcast_write:
            timeout = min(timeout, max(gap, 0.03))
        tries = 1 if broadcast_write else int(settings.get("retries") or 0) + 1
        tries = max(1, min(tries, 6))
        with self._lock:
            try:
                self._ensure(settings)
                last = None
                for _attempt in range(tries):
                    self._port.flush_input()
                    self._note("TX", frame)
                    self._port.write(frame)
                    raw, delay = self._port.read_until_silence(timeout, gap)
                    echo = bool(raw.startswith(frame)) if raw else False
                    if raw:
                        self._note("RX", raw, "эхо" if echo else "")
                    else:
                        self._note("RX", b"", "таймаут" if not broadcast_write else "broadcast")
                    last = extract_response(frame, raw, first_byte_delay=delay, echo_guard=echo_guard)
                    last["raw"] = raw
                    last["raw_hex"] = hex_bytes(raw)
                    last["delay"] = delay
                    last["timeout"] = not raw
                    last["broadcast"] = broadcast_write
                    if broadcast_write or last.get("frame"):
                        break
                return last
            except SerialError:
                self._port.close()
                self._settings = None
                raise
            finally:
                if not keep_open:
                    self._port.close()
                    self._settings = None
