"""Device profiles shipped as YAML. The loader covers the subset we write."""

import os
from pathlib import Path

from app.rtu import register_view

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = Path(os.environ.get("MODBUS_PROFILES", ROOT / "profiles"))


def _strip_comment(line: str) -> str:
    out = []
    quote = ""
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out).rstrip()


def _split_key(text: str):
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == ":":
            return text[:index].strip(), text[index + 1 :].strip()
    raise ValueError(f"ожидался ключ: {text}")


def parse_scalar(text: str):
    value = text.strip()
    if value in ("", "null", "~"):
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part) for part in _split_csv(inner)]
    if value[:2].lower() == "0x":
        return int(value, 16)
    if value.lstrip("-").isdigit():
        return int(value)
    try:
        if any(char in value for char in ".eE") and not any(ch.isalpha() and ch not in "eE" for ch in value):
            return float(value)
    except ValueError:
        pass
    return value


def _split_csv(text: str) -> list:
    parts = []
    buf = []
    quote = ""
    for char in text:
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            buf.append(char)
            continue
        if char == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    if buf:
        parts.append("".join(buf))
    return parts


def _unquote_key(key: str) -> str:
    key = key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        return key[1:-1]
    return key


def parse_yaml(text: str):
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cleaned = _strip_comment(raw)
        if cleaned.strip():
            lines.append(cleaned)
    value, index = _parse_block(lines, 0, 0)
    if index != len(lines):
        raise ValueError("лишние строки в профиле")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines, index, indent):
    if index >= len(lines) or _indent(lines[index]) < indent:
        return None, index
    if lines[index].lstrip().startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines, index, indent):
    node = {}
    while index < len(lines) and _indent(lines[index]) == indent and not lines[index].lstrip().startswith("- "):
        key, raw = _split_key(lines[index].lstrip())
        key = _unquote_key(key)
        index += 1
        if raw == "":
            if index < len(lines) and _indent(lines[index]) > indent:
                child, index = _parse_block(lines, index, _indent(lines[index]))
                node[key] = child
            else:
                node[key] = None
        else:
            node[key] = parse_scalar(raw)
    return node, index


def _parse_list(lines, index, indent):
    items = []
    while index < len(lines) and _indent(lines[index]) == indent and lines[index].lstrip().startswith("- "):
        content = lines[index].lstrip()[2:]
        index += 1
        child_indent = None
        nested = []
        while index < len(lines) and _indent(lines[index]) > indent:
            if child_indent is None:
                child_indent = _indent(lines[index])
            nested.append(lines[index])
            index += 1
        if not content:
            if nested:
                child, consumed = _parse_block(nested, 0, child_indent)
                if consumed != len(nested):
                    raise ValueError("битый список профиля")
                items.append(child)
            else:
                items.append(None)
            continue
        if content[0] in ("'", '"'):
            if nested:
                raise ValueError("скаляр с вложенным блоком")
            items.append(parse_scalar(content))
            continue
        if ":" in content:
            key, raw = _split_key(content)
            item = {}
            key = _unquote_key(key)
            if raw == "":
                if nested:
                    child, consumed = _parse_block(nested, 0, child_indent)
                    if consumed != len(nested):
                        raise ValueError("битый элемент профиля")
                    item[key] = child
                else:
                    item[key] = None
            else:
                item[key] = parse_scalar(raw)
                if nested:
                    extra, consumed = _parse_map(nested, 0, child_indent)
                    if consumed != len(nested):
                        raise ValueError("битый элемент профиля")
                    item.update(extra)
            items.append(item)
        else:
            if nested:
                raise ValueError("скаляр с вложенным блоком")
            items.append(parse_scalar(content))
    return items, index


def load_profiles() -> list:
    profiles = []
    if not PROFILE_DIR.is_dir():
        return profiles
    for path in sorted(PROFILE_DIR.glob("*.yaml")):
        profile = parse_yaml(path.read_text(encoding="utf-8"))
        profile["_file"] = path.name
        profiles.append(profile)
    return profiles


def profile_by_id(profiles, profile_id: str):
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile
    return None


def _enum_map(point: dict) -> dict:
    raw = point.get("enum") or {}
    mapped = {}
    for key, label in raw.items():
        try:
            mapped[int(key)] = label
        except (TypeError, ValueError):
            mapped[key] = label
    return mapped


def format_point(point: dict, raw_value: int) -> dict:
    decode = point.get("decode") or "uint16"
    if decode == "int16":
        numeric = raw_value - 0x10000 if raw_value & 0x8000 else raw_value
    elif point.get("kind") in ("coil", "discrete"):
        numeric = 1 if raw_value else 0
    else:
        numeric = raw_value
    invalid = point.get("invalid")
    if invalid is not None and int(raw_value) == int(invalid):
        text = point.get("invalid_text") or "нет датчика"
        return {"name": point.get("name"), "raw": raw_value, "text": text, "invalid": True}
    enum = _enum_map(point)
    if raw_value in enum:
        text = str(enum[raw_value])
    elif decode == "int16" and point.get("scale") not in (None, 1, 1.0):
        text = f"{numeric * float(point['scale']):.1f}"
    elif point.get("scale") not in (None, 1, 1.0):
        text = f"{numeric * float(point['scale']):g}"
    else:
        text = str(numeric)
    unit = point.get("unit") or ""
    if unit and not (invalid is not None and int(raw_value) == int(invalid)):
        text = f"{text} {unit}"
    view = register_view(raw_value) if point.get("kind") == "holding" else None
    return {"name": point.get("name"), "raw": raw_value, "text": text, "view": view, "invalid": False}


def decode_read(profile: dict, function: int, start: int, parsed: dict) -> list:
    if not profile or not parsed or not parsed.get("crc_ok"):
        return []
    rows = []
    points = profile.get("points") or []
    if function in (3, 4) and parsed.get("registers") is not None:
        kind = "holding" if function == 3 else "input"
        for point in points:
            if point.get("kind") != kind:
                continue
            index = int(point["address"]) - int(start)
            registers = parsed["registers"]
            if 0 <= index < len(registers):
                rows.append(format_point(point, registers[index]))
    if function in (1, 2) and parsed.get("bits") is not None:
        kind = "coil" if function == 1 else "discrete"
        for point in points:
            if point.get("kind") != kind:
                continue
            index = int(point["address"]) - int(start)
            bits = parsed["bits"]
            if 0 <= index < len(bits):
                rows.append(format_point(point, bits[index]))
    return rows


def public_profile(profile: dict) -> dict:
    data = {key: value for key, value in profile.items() if key != "_file"}
    return data
