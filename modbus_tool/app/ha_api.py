"""Call Home Assistant services through the Supervisor proxy."""

import json
import os
import urllib.error
import urllib.request


class HaError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HubControl:
    def __init__(self):
        # manual: user pressed «Освободить хаб» and the session stays stopped.
        self.manual = False
        self.last = None

    def stop(self, hub: str) -> dict:
        result = self._service("stop", {"hub": hub})
        self.last = result
        return result

    def reload(self) -> dict:
        # modbus.restart was removed in Home Assistant 2024.11.
        result = self._service("reload", {})
        self.manual = False
        self.last = result
        return result

    def _service(self, service: str, payload: dict) -> dict:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            raise HaError("нет SUPERVISOR_TOKEN: включите homeassistant_api у дополнения")
        url = f"http://supervisor/core/api/services/modbus/{service}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
                return {"ok": True, "service": f"modbus.{service}", "status": resp.status, "body": body[:500]}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise HaError(f"modbus.{service}: HTTP {exc.code} {detail}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise HaError(f"Supervisor API недоступен: {exc.reason}") from exc
