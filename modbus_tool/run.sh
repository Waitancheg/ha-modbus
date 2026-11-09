#!/bin/sh
cd /opt/modbus_tool || exit 1
exec python3 -m app.server
