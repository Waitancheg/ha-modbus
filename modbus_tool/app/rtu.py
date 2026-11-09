"""Modbus RTU frames, CRC16 and local-echo separation. No bus I/O."""

READ_FC = (1, 2, 3, 4)
WRITE_FC = (5, 6, 15, 16)
# Scan is read-only: holding reg 0, then coils, then discrete inputs.
PROBE_PLAN = ((3, 0, 1), (1, 0, 8), (2, 0, 8))

EXCEPTION_TEXT = {
    1: "недопустимая функция",
    2: "недопустимый адрес",
    3: "недопустимое значение",
    4: "сбой устройства",
    5: "подтверждение",
    6: "устройство занято",
    8: "ошибка чётности",
    10: "шлюз: нет пути",
    11: "шлюз: нет ответа",
}


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(payload: bytes) -> bytes:
    crc = crc16(payload)
    return payload + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def crc_ok(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    expect = crc16(frame[:-2])
    got = frame[-2] | (frame[-1] << 8)
    return expect == got


def _u16(value: int) -> bytes:
    value &= 0xFFFF
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


def build_read(address: int, function: int, start: int, quantity: int) -> bytes:
    if function not in READ_FC:
        raise ValueError("функция чтения")
    if quantity < 1:
        raise ValueError("количество")
    return append_crc(bytes((address & 0xFF, function)) + _u16(start) + _u16(quantity))


def build_write_coil(address: int, coil: int, on: bool) -> bytes:
    # Modbus ON is 0xFF00, OFF is 0x0000. Any other value is illegal.
    value = 0xFF00 if on else 0x0000
    return append_crc(bytes((address & 0xFF, 5)) + _u16(coil) + _u16(value))


def build_write_register(address: int, register: int, value: int) -> bytes:
    return append_crc(bytes((address & 0xFF, 6)) + _u16(register) + _u16(value))


def build_write_coils(address: int, start: int, coils: list) -> bytes:
    quantity = len(coils)
    if not 1 <= quantity <= 1968:
        raise ValueError("количество катушек")
    packed = bytearray((quantity + 7) // 8)
    for index, bit in enumerate(coils):
        if bit:
            packed[index // 8] |= 1 << (index % 8)
    body = bytes((address & 0xFF, 0x0F)) + _u16(start) + _u16(quantity)
    body += bytes((len(packed),)) + bytes(packed)
    return append_crc(body)


def build_write_registers(address: int, start: int, values: list) -> bytes:
    quantity = len(values)
    if not 1 <= quantity <= 123:
        raise ValueError("количество регистров")
    data = b"".join(_u16(int(value)) for value in values)
    body = bytes((address & 0xFF, 0x10)) + _u16(start) + _u16(quantity)
    body += bytes((len(data),)) + data
    return append_crc(body)


def build_broadcast_address(new_address: int) -> bytes:
    """FC16 to slave 0, register 0: set the relay-board address.

    Field frame for address 20 (0x14): 00 10 00 00 00 01 02 00 14 + CRC.
    """
    if not 1 <= int(new_address) <= 255:
        raise ValueError("адрес 1..255")
    return build_write_registers(0, 0, [int(new_address)])


def hex_bytes(data: bytes) -> str:
    return data.hex(" ") if data else ""


def parse_hex(text: str) -> bytes:
    cleaned = []
    for char in text.replace("0x", " ").replace("0X", " "):
        if char in " \t\r\n,:;-_":
            cleaned.append(" ")
        elif char in "0123456789abcdefABCDEF":
            cleaned.append(char)
        else:
            raise ValueError("в HEX допустимы только цифры 0-9 и A-F")
    blob = "".join(cleaned).replace(" ", "")
    if not blob:
        raise ValueError("пустой кадр")
    if len(blob) % 2:
        raise ValueError("нечётное число полубайтов")
    return bytes.fromhex(blob)


def register_view(value: int) -> dict:
    value &= 0xFFFF
    signed = value - 0x10000 if value & 0x8000 else value
    return {
        "dec": value,
        "hex": f"{value:04X}",
        "signed": signed,
        "bin": f"{value:016b}",
    }


def parse_frame(frame: bytes) -> dict:
    result = {
        "crc_ok": crc_ok(frame),
        "hex": hex_bytes(frame),
        "address": frame[0] if frame else None,
        "fc": None,
        "exception": False,
    }
    if not result["crc_ok"]:
        return result
    function = frame[1]
    if function & 0x80:
        code = frame[2] if len(frame) >= 5 else None
        result.update(
            {
                "fc": function & 0x7F,
                "exception": True,
                "exception_code": code,
                "exception_text": EXCEPTION_TEXT.get(code, "исключение"),
            }
        )
        return result
    result["fc"] = function
    if function in (1, 2) and len(frame) >= 5:
        count = frame[2]
        data = frame[3 : 3 + count]
        bits = []
        for byte in data:
            for bit in range(8):
                bits.append((byte >> bit) & 1)
        result["bits"] = bits
        result["byte_count"] = count
    elif function in (3, 4) and len(frame) >= 5:
        count = frame[2]
        data = frame[3 : 3 + count]
        registers = []
        for index in range(0, len(data) - 1, 2):
            registers.append((data[index] << 8) | data[index + 1])
        result["registers"] = registers
        result["views"] = [register_view(item) for item in registers]
        result["byte_count"] = count
    elif function in WRITE_FC and len(frame) >= 6:
        result["echo_address"] = (frame[2] << 8) | frame[3]
        result["echo_value"] = (frame[4] << 8) | frame[5]
    return result


def _expected_lengths(function: int, quantity: int) -> list:
    lengths = [5]
    if function in (1, 2):
        lengths.append(5 + ((max(quantity, 1) + 7) // 8))
    elif function in (3, 4):
        lengths.append(5 + max(quantity, 1) * 2)
    elif function in WRITE_FC:
        lengths.append(8)
    return lengths


def _first_crc_frame(buf: bytes, skip: bytes | None = None) -> bytes | None:
    index = 0
    size = len(buf)
    while index < size:
        found = None
        for length in range(4, size - index + 1):
            chunk = buf[index : index + length]
            if crc_ok(chunk) and chunk != skip:
                found = chunk
                break
        if found is not None:
            return found
        index += 1
    return None


def _match_expected(sent: bytes, payload: bytes) -> bytes | None:
    if len(sent) < 2 or not payload:
        return None
    function = sent[1]
    quantity = ((sent[4] << 8) | sent[5]) if len(sent) >= 6 else 1
    if len(payload) >= 5 and payload[1] & 0x80 and crc_ok(payload[:5]):
        return payload[:5]
    normal = [n for n in _expected_lengths(function, quantity) if n != 5]
    for length in normal:
        if len(payload) < length or not crc_ok(payload[:length]):
            continue
        chunk = payload[:length]
        if chunk == sent and function not in WRITE_FC:
            continue
        return chunk
    return _first_crc_frame(payload, skip=None if function in WRITE_FC else sent)


def extract_response(
    sent: bytes,
    received: bytes,
    first_byte_delay: float | None = None,
    echo_guard: float = 0.005,
) -> dict:
    """Drop a USB-RS485 echo of the request and return the device frame.

    A read reply never equals the request, so one copy of a read is echo.
    A write reply repeats the request. One copy is the device when it arrives
    slower than echo_guard; faster than that is the adapter looping TX into RX.
    Two copies are echo plus the real reply. echo_guard <= 0 disables the
    timing test (a single write copy is kept).
    """
    empty = {
        "frame": None,
        "payload": b"",
        "echo": False,
        "parsed": None,
        "crc_ok": False,
    }
    if not received:
        return empty

    function = sent[1] if len(sent) >= 2 else 0
    is_write = function in WRITE_FC
    echoed = False
    payload = received

    if sent and (received.startswith(sent + sent) or (received.startswith(sent) and received != sent)):
        echoed = True
        payload = received[len(sent) :]
        frame = _match_expected(sent, payload)
        if frame is None:
            frame = _first_crc_frame(payload, skip=None if is_write else sent)
    elif sent and received == sent:
        fast = first_byte_delay is not None and echo_guard > 0 and first_byte_delay < echo_guard
        if (not is_write) or fast:
            echoed = True
            payload = b""
            frame = None
        else:
            frame = sent
            payload = sent
    else:
        frame = _match_expected(sent, received)
        if frame is None:
            frame = _first_crc_frame(received, skip=sent if not is_write else None)
        if frame is None and sent and sent in received:
            echoed = True
            payload = received[received.find(sent) + len(sent) :]
            frame = _match_expected(sent, payload) or _first_crc_frame(payload)

    parsed = parse_frame(frame) if frame else None
    return {
        "frame": frame,
        "payload": payload,
        "echo": echoed,
        "parsed": parsed,
        "crc_ok": bool(parsed and parsed.get("crc_ok")),
    }


def probe_device(transact, address: int) -> dict:
    """Read-only presence check. An exception response counts as present."""
    attempts = []
    for function, start, quantity in PROBE_PLAN:
        frame = build_read(address, function, start, quantity)
        if frame[1] not in READ_FC:
            raise RuntimeError("сканирование пыталось записать на шину")
        result = transact(frame)
        parsed = result.get("parsed") or {}
        attempts.append(
            {
                "fc": function,
                "hex": hex_bytes(frame),
                "crc_ok": bool(result.get("crc_ok")),
                "exception": bool(parsed.get("exception")),
                "echo": bool(result.get("echo")),
            }
        )
        same = parsed.get("address") == (address & 0xFF)
        if parsed.get("crc_ok") and same and (parsed.get("exception") or parsed.get("fc") == function):
            return {
                "present": True,
                "address": address,
                "fc": function,
                "exception": bool(parsed.get("exception")),
                "exception_code": parsed.get("exception_code"),
                "exception_text": parsed.get("exception_text"),
                "attempts": attempts,
            }
    return {"present": False, "address": address, "attempts": attempts}


def auto_gap(baud: int, parity: str, stopbits: int) -> float:
    bits = 1 + 8 + (0 if parity == "N" else 1) + int(stopbits)
    # USB-UART вставляет паузы длиннее спецификации RTU (3.5 символа) и рвёт кадр.
    return max((bits / float(baud)) * 3.5, 0.015)
