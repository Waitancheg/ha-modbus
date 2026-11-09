import os
import pty
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rtu import append_crc, build_read, build_write_coil, extract_response, probe_device
from app.serial_port import SerialPort


def _read_response(address, value=0x0100):
    return append_crc(bytes((address, 3, 2, (value >> 8) & 0xFF, value & 0xFF)))


class EchoTests(unittest.TestCase):
    def test_strips_leading_echo_from_read(self):
        sent = build_read(1, 3, 0, 1)
        resp = _read_response(1, 0x00FA)
        parsed = extract_response(sent, sent + resp, first_byte_delay=0.0002, echo_guard=0.005)
        self.assertTrue(parsed["echo"])
        self.assertEqual(parsed["frame"], resp)
        self.assertEqual(parsed["parsed"]["registers"], [0x00FA])

    def test_read_without_echo(self):
        sent = build_read(2, 3, 0, 1)
        resp = _read_response(2, 10)
        parsed = extract_response(sent, resp, first_byte_delay=0.02, echo_guard=0.005)
        self.assertFalse(parsed["echo"])
        self.assertEqual(parsed["frame"], resp)

    def test_read_echo_only_is_not_a_device(self):
        sent = build_read(3, 3, 0, 1)
        parsed = extract_response(sent, sent, first_byte_delay=0.0001, echo_guard=0.005)
        self.assertTrue(parsed["echo"])
        self.assertIsNone(parsed["frame"])
        self.assertFalse(parsed["crc_ok"])

    def test_fast_write_copy_is_echo_slow_copy_is_reply(self):
        sent = build_write_coil(4, 1, True)
        fast = extract_response(sent, sent, first_byte_delay=0.0004, echo_guard=0.005)
        slow = extract_response(sent, sent, first_byte_delay=0.02, echo_guard=0.005)
        self.assertIsNone(fast["frame"])
        self.assertTrue(fast["echo"])
        self.assertEqual(slow["frame"], sent)
        self.assertFalse(slow["echo"])
        disabled = extract_response(sent, sent, first_byte_delay=0.0001, echo_guard=0)
        self.assertEqual(disabled["frame"], sent)

    def test_doubled_write_keeps_the_second_copy(self):
        sent = build_write_coil(4, 0, False)
        parsed = extract_response(sent, sent + sent, first_byte_delay=0.0001, echo_guard=0.005)
        self.assertTrue(parsed["echo"])
        self.assertEqual(parsed["frame"], sent)
        self.assertEqual(parsed["parsed"]["echo_value"], 0)

    def test_malformed_broadcast_is_kept_as_payload(self):
        sent = build_read(0, 3, 0, 1)
        junk = bytes.fromhex("00ff01")
        parsed = extract_response(sent, sent + junk, first_byte_delay=0.0002, echo_guard=0.005)
        self.assertTrue(parsed["echo"])
        self.assertIsNone(parsed["frame"])
        self.assertEqual(parsed["payload"], junk)
        self.assertFalse(parsed["crc_ok"])

    def test_probe_ignores_echo_and_accepts_real_frame(self):
        def transact(frame):
            resp = _read_response(frame[0], 1)
            return extract_response(frame, frame + resp, first_byte_delay=0.0002, echo_guard=0.005)

        result = probe_device(transact, 9)
        self.assertTrue(result["present"])
        self.assertEqual(result["fc"], 3)
        self.assertTrue(result["attempts"][0]["echo"])

    def test_pty_strips_adapter_echo(self):
        master, slave = pty.openpty()
        port = SerialPort()
        try:
            port.configure(os.ttyname(slave), 9600, "N", 1)
            frame = build_read(1, 3, 0, 1)
            resp = append_crc(bytes((1, 3, 2, 0, 10)))

            def peer():
                data = b""
                while len(data) < len(frame):
                    data += os.read(master, 64)
                os.write(master, data + resp)

            thread = threading.Thread(target=peer, daemon=True)
            thread.start()
            port.write(frame)
            raw, delay = port.read_until_silence(1.0, 0.02)
            thread.join(1)
            parsed = extract_response(frame, raw, delay, 0.005)
        finally:
            port.close()
            os.close(master)
            os.close(slave)
        self.assertTrue(parsed["echo"])
        self.assertEqual(parsed["frame"], resp)
        self.assertEqual(parsed["parsed"]["registers"], [10])

    def test_fast_unrelated_bytes_are_not_echo(self):
        sent = build_read(1, 3, 0, 1)
        other = _read_response(1, 1)
        parsed = extract_response(sent, other, first_byte_delay=0.0001, echo_guard=0.005)
        self.assertFalse(parsed["echo"])
        self.assertEqual(parsed["frame"], other)

    def test_close_releases_exclusive_lock(self):
        import fcntl
        import termios
        from app import serial_port as serial_mod

        master, slave = pty.openpty()
        calls = []
        real = serial_mod.fcntl.ioctl

        def spy(fd, request, *args):
            if request == termios.TIOCNXCL:
                os.fstat(fd)
            calls.append(request)
            return real(fd, request, *args)

        serial_mod.fcntl.ioctl = spy
        port = SerialPort()
        try:
            port.configure(os.ttyname(slave), 9600, "N", 1)
            port.close()
        finally:
            serial_mod.fcntl.ioctl = real
            os.close(master)
            try:
                os.close(slave)
            except OSError:
                pass
        self.assertIn(termios.TIOCEXCL, calls)
        self.assertIn(termios.TIOCNXCL, calls)
        self.assertLess(calls.index(termios.TIOCEXCL), calls.index(termios.TIOCNXCL))

    def test_noise_after_echo_is_absent(self):
        def transact(frame):
            return extract_response(frame, frame + b"\x01\x02\x03", 0.0002, 0.005)

        result = probe_device(transact, 9)
        self.assertFalse(result["present"])
        self.assertEqual(len(result["attempts"]), 3)
        self.assertTrue(all(item["fc"] in (1, 2, 3) for item in result["attempts"]))


if __name__ == "__main__":
    unittest.main()
