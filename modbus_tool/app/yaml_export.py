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
    slave = int(settings.get("address"))
    lines = []
    for note in profile.get("notes") or []:
        lines.append(f"# {note}\n")
    lines.append(_line(0, "modbus:"))
    lines.append(_line(1, f"- name: {_q(hub)}"))
    lines.append(_line(2, "type: serial"))
    lines.append(_line(2, f"port: {_q(port)}"))
    lines.append(_line(2, f"baudrate: {baud}"))
    lines.append(_line(2, "bytesize: 8"))
    lines.append(_line(2, "method: rtu"))
    lines.append(_line(2, f"parity: {_q(parity)}"))
    lines.append(_line(2, f"stopbits: {stopbits}"))
    lines.append(_line(2, "timeout: 1"))

    sensors = []
    switches = []
    binary = []
    for point in profile.get("points") or []:
        export = point.get("export")
        if export == "sensor":
            sensors.append(point)
        elif export == "switch":
            switches.append(point)
        elif export == "binary_sensor":
            binary.append(point)

    if sensors:
        lines.append(_line(2, "sensors:"))
        for point in sensors:
            lines.extend(_sensor(point, slave))
    if switches:
        lines.append(_line(2, "switches:"))
        for point in switches:
            lines.extend(_switch(point, slave))
    if binary:
        lines.append(_line(2, "binary_sensors:"))
        for point in binary:
            lines.extend(_binary(point, slave))
    return "".join(lines)


def _sensor(point: dict, slave: int) -> list:
    data_type = "int16" if point.get("decode") == "int16" else "uint16"
    rows = [
        _line(3, f"- name: {_q(point['name'])}"),
        _line(4, f"device_address: {slave}"),
        _line(4, f"address: {int(point['address'])}"),
        _line(4, "input_type: holding"),
        _line(4, f"data_type: {data_type}"),
    ]
    if point.get("scale") not in (None, 1, 1.0):
        scale = float(point["scale"])
        rows.append(_line(4, f"scale: {scale}"))
        rows.append(_line(4, f"precision: {int(point.get('precision') or 1)}"))
    if point.get("unit"):
        rows.append(_line(4, f"unit_of_measurement: {_q(point['unit'])}"))
    if point.get("device_class"):
        rows.append(_line(4, f"device_class: {point['device_class']}"))
    if point.get("state_class"):
        rows.append(_line(4, f"state_class: {point['state_class']}"))
    if point.get("invalid") is not None:
        rows.append(_line(4, f"nan_value: {int(point['invalid'])}"))
    return rows


def _switch(point: dict, slave: int) -> list:
    address = int(point["address"])
    return [
        _line(3, f"- name: {_q(point['name'])}"),
        _line(4, f"device_address: {slave}"),
        _line(4, f"address: {address}"),
        _line(4, "write_type: coil"),
        _line(4, "command_on: 1"),
        _line(4, "command_off: 0"),
        _line(4, "verify:"),
        _line(5, "input_type: coil"),
        _line(5, f"address: {address}"),
    ]


def _binary(point: dict, slave: int) -> list:
    rows = [
        _line(3, f"- name: {_q(point['name'])}"),
        _line(4, f"device_address: {slave}"),
        _line(4, f"address: {int(point['address'])}"),
        _line(4, "input_type: discrete_input"),
    ]
    if point.get("slave_count") is not None:
        rows.append(_line(4, f"slave_count: {int(point['slave_count'])}"))
    if point.get("device_class"):
        rows.append(_line(4, f"device_class: {point['device_class']}"))
    return rows
