"""Build a Home Assistant modbus: snippet from a profile."""


def _q(value) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _indent(level: int) -> str:
    return "  " * level


def _line(level: int, text: str) -> str:
    return f"{_indent(level)}{text}\n"


def generate_yaml(profile: dict, settings: dict) -> str:
    hub = settings.get("hub") or "modbus_hub"
    port = settings.get("device") or "/dev/ttyUSB0"
    baud = int(settings.get("baudrate") or profile.get("defaults", {}).get("baudrate") or 9600)
    parity = settings.get("parity") or profile.get("defaults", {}).get("parity") or "N"
    stopbits = int(settings.get("stopbits") or profile.get("defaults", {}).get("stopbits") or 1)
    bytesize = int(settings.get("bytesize") or 8)
    slave = int(settings.get("address"))
    full = bool(settings.get("full_hub"))
    channels = settings.get("channels")
    channels = int(channels) if channels not in (None, "") else None
    base = 2 if full else 0
    lines = []
    for note in profile.get("notes") or []:
        lines.append(f"# {note}\n")
    if full:
        lines.append(_line(0, "modbus:"))
        lines.append(_line(1, f"- name: {_q(hub)}"))
        lines.append(_line(2, "type: serial"))
        lines.append(_line(2, f"port: {_q(port)}"))
        lines.append(_line(2, f"baudrate: {baud}"))
        lines.append(_line(2, f"bytesize: {bytesize}"))
        lines.append(_line(2, "method: rtu"))
        lines.append(_line(2, f"parity: {_q(parity)}"))
        lines.append(_line(2, f"stopbits: {stopbits}"))
        lines.append(_line(2, "timeout: 1"))

    sensors, switches, binary = [], [], []
    for point in profile.get("points") or []:
        if channels is not None and point.get("channel") not in (None, "") and int(point["channel"]) > channels:
            continue
        export = point.get("export")
        if export == "sensor":
            sensors.append(point)
        elif export == "switch":
            switches.append(point)
        elif export == "binary_sensor":
            binary.append(point)

    if sensors:
        lines.append(_line(base, "sensors:"))
        for point in sensors:
            lines.extend(_sensor(point, slave, base))
    if switches:
        lines.append(_line(base, "switches:"))
        for point in switches:
            lines.extend(_switch(point, slave, base))
    if binary:
        lines.append(_line(base, "binary_sensors:"))
        for point in binary:
            lines.extend(_binary(point, slave, base, channels))
    return "".join(lines)


def _sensor(point: dict, slave: int, base: int = 2) -> list:
    data_type = "int16" if point.get("decode") == "int16" else "uint16"
    item, field = base + 1, base + 2
    rows = [
        _line(item, f"- name: {_q(point['name'])}"),
        _line(field, f"device_address: {slave}"),
        _line(field, f"address: {int(point['address'])}"),
        _line(field, "input_type: holding"),
        _line(field, f"data_type: {data_type}"),
    ]
    if point.get("scale") not in (None, 1, 1.0):
        scale = float(point["scale"])
        rows.append(_line(field, f"scale: {scale}"))
        rows.append(_line(field, f"precision: {int(point.get('precision') or 1)}"))
    if point.get("unit"):
        rows.append(_line(field, f"unit_of_measurement: {_q(point['unit'])}"))
    if point.get("device_class"):
        rows.append(_line(field, f"device_class: {point['device_class']}"))
    if point.get("state_class"):
        rows.append(_line(field, f"state_class: {point['state_class']}"))
    if point.get("invalid") is not None:
        rows.append(_line(field, f"nan_value: {int(point['invalid'])}"))
    return rows


def _switch(point: dict, slave: int, base: int = 2) -> list:
    address = int(point["address"])
    item, field = base + 1, base + 2
    return [
        _line(item, f"- name: {_q(point['name'])}"),
        _line(field, f"device_address: {slave}"),
        _line(field, f"address: {address}"),
        _line(field, "write_type: coil"),
        _line(field, "command_on: 1"),
        _line(field, "command_off: 0"),
        _line(field, "verify:"),
        _line(field + 1, "input_type: coil"),
        _line(field + 1, f"address: {address}"),
    ]


def _binary(point: dict, slave: int, base: int = 2, channels: int | None = None) -> list:
    item, field = base + 1, base + 2
    rows = [
        _line(item, f"- name: {_q(point['name'])}"),
        _line(field, f"device_address: {slave}"),
        _line(field, f"address: {int(point['address'])}"),
        _line(field, "input_type: discrete_input"),
    ]
    if point.get("slave_count") is not None:
        count = int(point["slave_count"]) if channels is None else max(0, int(channels) - 1)
        if count:
            rows.append(_line(field, f"slave_count: {count}"))
    if point.get("device_class"):
        rows.append(_line(field, f"device_class: {point['device_class']}"))
    return rows
