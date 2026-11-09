import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.profiles import format_point, load_profiles
from app.rtu import (
    PROBE_PLAN,
    auto_gap,
    append_crc,
    build_broadcast_address,
    build_read,
    build_write_coil,
    build_write_coils,
    build_write_register,
    build_write_registers,
    crc_ok,
    parse_frame,
    probe_device,
    register_view,
)
from app.yaml_export import generate_yaml


def _silent(frame):
    return {"parsed": None, "crc_ok": False, "echo": False}


class FramingTests(unittest.TestCase):
    def test_auto_gap_is_at_least_usb_pause(self):
        self.assertAlmostEqual(auto_gap(9600, "N", 1), 0.015, places=6)
        self.assertAlmostEqual(auto_gap(1200, "N", 1), 10 / 1200 * 3.5, places=6)

    def test_probe_plan_is_read_only_and_includes_255(self):
        self.assertEqual(PROBE_PLAN, ((3, 0, 1), (1, 0, 8), (2, 0, 8)))
        frames = [build_read(255, fc, start, qty) for fc, start, qty in PROBE_PLAN]
        self.assertEqual([frame[1] for frame in frames], [3, 1, 2])
        self.assertTrue(all(frame[0] == 255 and crc_ok(frame) for frame in frames))
        self.assertEqual(frames[0][4:6], b"\x00\x01")
        self.assertEqual(frames[1][4:6], b"\x00\x08")
        self.assertEqual(frames[2][4:6], b"\x00\x08")

    def test_scan_stops_on_exception_and_never_writes(self):
        seen = []

        def transact(frame):
            seen.append(frame)
            exc = append_crc(bytes((frame[0], frame[1] | 0x80, 0x02)))
            return {"parsed": parse_frame(exc), "crc_ok": True, "echo": False, "frame": exc}

        result = probe_device(transact, 255)
        self.assertTrue(result["present"])
        self.assertTrue(result["exception"])
        self.assertEqual(result["exception_code"], 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], 3)

    def test_scan_falls_back_after_timeout(self):
        seen = []

        def transact(frame):
            seen.append(frame[1])
            if frame[1] != 1:
                return _silent(frame)
            resp = append_crc(bytes((frame[0], 1, 1, 0x01)))
            return {"parsed": parse_frame(resp), "crc_ok": True, "echo": False, "frame": resp}

        result = probe_device(transact, 10)
        self.assertEqual(seen, [3, 1])
        self.assertTrue(result["present"])
        self.assertFalse(result["exception"])
        self.assertEqual(result["fc"], 1)

    def test_coil_and_register_writes(self):
        on = build_write_coil(255, 0, True)
        off = build_write_coil(255, 3, False)
        self.assertEqual(on[:6], bytes.fromhex("ff050000ff00"))
        self.assertEqual(off[:6], bytes.fromhex("ff0500030000"))
        self.assertTrue(crc_ok(on) and crc_ok(off))
        packed = build_write_coils(1, 0, [1, 0, 1, 1])
        self.assertEqual(packed[6:8], bytes((1, 0x0D)))
        regs = build_write_registers(1, 1001, [3])
        self.assertEqual(regs[1], 0x10)
        self.assertEqual(regs[2:6], bytes.fromhex("03e90001"))
        self.assertEqual(regs[6:9], bytes.fromhex("020003"))
        self.assertTrue(crc_ok(regs))

    def test_broadcast_address_frame(self):
        frame = build_broadcast_address(0x14)
        self.assertEqual(frame[:-2], bytes.fromhex("001000000001020014"))
        self.assertTrue(crc_ok(frame))
        self.assertEqual(build_write_register(1, 2, 20)[1], 6)

    def test_views_and_missing_sensor(self):
        self.assertEqual(register_view(0x8000)["signed"], -32768)
        self.assertEqual(register_view(0xFFFF)["signed"], -1)
        self.assertEqual(register_view(10)["hex"], "000A")
        text = format_point({"name": "Температура 1", "decode": "int16", "scale": 0.1, "unit": "°C", "invalid": 32768, "invalid_text": "нет датчика", "kind": "holding"}, 32768)
        self.assertEqual(text["text"], "нет датчика")
        warm = format_point({"name": "Температура 1", "decode": "int16", "scale": 0.1, "unit": "°C", "invalid": 32768, "kind": "holding"}, 255)
        self.assertEqual(warm["text"], "25.5 °C")

    def test_profiles_match_field_notes(self):
        profiles = {item["id"]: item for item in load_profiles()}
        self.assertEqual(set(profiles), {"eletechsup_n4dsa02", "chinese_relay_2_4ch", "r4dcb08"})
        n4 = profiles["eletechsup_n4dsa02"]
        self.assertFalse(any(int(point["address"]) == 255 for point in n4["points"]))
        baud = next(point for point in n4["points"] if point["address"] == 3)
        self.assertIn(5, baud["forbid"])
        self.assertEqual(baud["enum"]["4"], "19200")
        relay = profiles["chinese_relay_2_4ch"]
        self.assertEqual(relay["defaults"]["address"], 255)
        self.assertEqual(relay["defaults"]["baudrate"], 9600)
        self.assertEqual(relay["defaults"]["parity"], "N")
        inputs = next(item for item in relay["actions"] if item["id"] == "read_inputs")
        self.assertEqual((inputs["fc"], inputs["quantity"]), (2, 8))
        relays = next(item for item in relay["actions"] if item["id"] == "read_relays")
        self.assertEqual((relays["fc"], relays["quantity"]), (1, 8))
        baud_action = next(item for item in relay["actions"] if item["id"] == "set_baud")
        self.assertEqual(baud_action["address"], 1001)
        self.assertEqual(baud_action["fc"], 16)
        self.assertEqual(next(item["op"] for item in relay["actions"] if item["id"] == "set_address"), "broadcast_address")
        temps = [point for point in profiles["r4dcb08"]["points"] if point["export"] == "sensor"]
        self.assertEqual(len(temps), 8)
        self.assertTrue(all(point["decode"] == "int16" and point["scale"] == 0.1 for point in temps))
        self.assertEqual([point["address"] for point in temps], list(range(8)))

    def test_yaml_snippet(self):
        profiles = {item["id"]: item for item in load_profiles()}
        text = generate_yaml(profiles["eletechsup_n4dsa02"], {
            "hub": "hub",
            "device": "/dev/ttyUSB0",
            "baudrate": 9600,
            "parity": "N",
            "stopbits": 1,
            "address": 4,
            "full_hub": True,
        })
        self.assertIn("device_address: 4", text)
        self.assertIn("nan_value: 32768", text)
        self.assertIn("data_type: int16", text)
        self.assertIn("scale: 0.1", text)
        self.assertIn('method: rtu', text)
        relay = generate_yaml(profiles["chinese_relay_2_4ch"], {
            "hub": "hub",
            "device": "/dev/serial/by-id/usb-adapter",
            "baudrate": 9600,
            "parity": "N",
            "stopbits": 1,
            "address": 255,
        })
        self.assertIn("write_type: coil", relay)
        self.assertIn("command_on: 1", relay)
        self.assertIn("slave_count: 7", relay)
        self.assertIn("input_type: discrete_input", relay)
        self.assertIn("device_address: 255", relay)
        narrow = generate_yaml(profiles["chinese_relay_2_4ch"], {
            "address": 255,
            "channels": 2,
            "full_hub": False,
        })
        self.assertNotIn("\nmodbus:", "\n" + narrow)
        self.assertIn("slave_count: 1", narrow)
        self.assertIn('name: "Реле 1"', narrow)
        self.assertNotIn('name: "Реле 5"', narrow)
        self.assertNotIn('name: "Реле 3 режим"', narrow)


if __name__ == "__main__":
    unittest.main()
