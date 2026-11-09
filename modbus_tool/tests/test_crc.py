import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rtu import append_crc, build_read, crc16, crc_ok


class CrcTests(unittest.TestCase):
    def test_broadcast_read_vector(self):
        # Кадр чтения адреса relay-платы: 00 03 00 00 00 01 85 DB.
        frame = build_read(0, 3, 0, 1)
        self.assertEqual(frame.hex(), "00030000000185db")
        self.assertEqual(append_crc(bytes.fromhex("010300000001")).hex(), "010300000001840a")
        self.assertEqual(crc16(frame[:-2]), 0xDB85)
        self.assertTrue(crc_ok(frame))

    def test_crc_is_little_endian_and_stable(self):
        payload = bytes.fromhex("010300000001")
        frame = append_crc(payload)
        self.assertEqual(frame[-2] | (frame[-1] << 8), crc16(payload))
        self.assertNotEqual(crc16(payload + b"\x00"), crc16(payload))
        self.assertFalse(crc_ok(frame[:-1] + bytes((frame[-1] ^ 0xFF,))))

    def test_table_matches_bitwise(self):
        table = []
        for value in range(256):
            crc = value
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
            table.append(crc)
        data = bytes(range(32))

        def via_table(buf):
            crc = 0xFFFF
            for byte in buf:
                crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
            return crc

        self.assertEqual(via_table(data), crc16(data))


if __name__ == "__main__":
    unittest.main()
