ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

RUN if ! command -v python3 >/dev/null 2>&1; then apk add --no-cache python3; fi

WORKDIR /opt/modbus_tool
COPY app ./app
COPY profiles ./profiles
COPY run.sh /run.sh
COPY run.sh /etc/services.d/modbus_tool/run
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /run.sh /entrypoint.sh /etc/services.d/modbus_tool/run

ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64
LABEL \
  io.hass.name="Modbus RTU Tool" \
  io.hass.description="Сканирование и настройка Modbus RTU на RS485" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.type="addon" \
  io.hass.version="${BUILD_VERSION}" \
  org.opencontainers.image.title="Modbus RTU Tool" \
  org.opencontainers.image.source="https://github.com/Waitancheg/ha-modbus" \
  org.opencontainers.image.licenses="MIT"

EXPOSE 8099
ENTRYPOINT ["/entrypoint.sh"]
