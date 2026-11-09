# Modbus RTU Tool

Репозиторий дополнения Home Assistant для сканирования и настройки устройств Modbus RTU на RS485 (USB-адаптер, `/dev/ttyUSB0`, `/dev/serial/by-id/…`). Интерфейс открывается из боковой панели, консоль не нужна.

## Установка

1. **Настройки → Дополнения → Магазин дополнений → ⋮ → Репозитории**.
2. Добавьте `https://github.com/Waitancheg/ha-modbus`.
3. Установите **Modbus RTU Tool** (сборка на устройстве из `python:3.12-alpine`).
4. В конфигурации укажите последовательный порт и имя хаба `modbus` из `configuration.yaml`.
5. Запустите дополнение и откройте **Modbus RTU** в боковом меню.

Перед работой с шиной дополнение может вызвать `modbus.stop`, а после — `modbus.restart` (если этой службы уже нет — `modbus.reload`), чтобы отпустить порт, который держит встроенная интеграция.

Подробности, профили плат и ограничения: [modbus_tool/README.md](modbus_tool/README.md) и [modbus_tool/DOCS.md](modbus_tool/DOCS.md).

```bash
python3 -m unittest discover -s modbus_tool/tests -v
```
