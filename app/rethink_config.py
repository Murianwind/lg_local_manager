"""
rethink-config.json 을 만들고 검증한다.

사용자에게 직접 파일을 만들고 스키마를 채우게 하는 대신, 최초 실행 시 값이
빠져 있으면 통합 웹 UI(webui.py)에서 몇 가지 값만 입력받아 이 모듈이 전체
config를 만들어낸다. 나머지 값(포트 등)은 rethink 원본 config.jsonc의 권장
기본값을 그대로 쓴다 — 443/8883에서 벗어나면 리다이렉트 이후 기기가 광고받은
새 포트로 옮겨가면서 우리 WinDivert 필터를 벗어날 수 있어서, 일부러 건드리지
않는다.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import TypedDict


class RethinkSetupInput(TypedDict):
    hostname: str
    mqtt_host: str
    mqtt_port: str
    mqtt_user: str
    mqtt_pass: str


DEFAULT_HOSTNAME = "rethink.lan"


def is_configured(config_path: Path) -> bool:
    """rethink-config.json이 존재하고, 필수 값(MQTT 브로커 주소)이 채워져 있는지."""
    if not config_path.exists():
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    mqtt_url = data.get("homeassistant", {}).get("mqtt_url", "")
    # "mqtt://192.168.0.X:1883" 같은 플레이스홀더 상태는 미설정으로 취급한다.
    return bool(mqtt_url) and "192.168.0.X" not in mqtt_url


def build_config(values: RethinkSetupInput) -> dict:
    """입력값 + 고정 기본값으로 전체 rethink config 딕셔너리를 만든다."""
    hostname = values.get("hostname", "").strip() or DEFAULT_HOSTNAME
    mqtt_host = values["mqtt_host"].strip()
    mqtt_port = values.get("mqtt_port", "").strip() or "1883"
    mqtt_user = values.get("mqtt_user", "").strip()
    mqtt_pass = values.get("mqtt_pass", "").strip()

    return {
        "hostname": hostname,
        "advertise_requested_host": True,
        "homeassistant": {
            "mqtt_url": f"mqtt://{mqtt_host}:{mqtt_port}",
            "discovery_prefix": "homeassistant",
            "rethink_prefix": "rethink",
            "mqtt_user": mqtt_user,
            "mqtt_pass": mqtt_pass,
        },
        "ca_key_file": "ca.key",
        "ca_cert_file": "ca.cert",
        # 기기 네이티브 기대값 그대로 — 리다이렉트 이후 포트가 안 바뀌게 함.
        "https_port": 443,
        "mqtts_port": 8883,
        "mqtt_port": 1884,
        "thinq1_https_port": 46030,
        "thinq1_port": 47878,
        "management_port": 44401,
        "bridge": {"storage_path": "./state"},
        # "MGMT"는 rethink 자체 관리 웹 UI(대시보드 등)를 볼 때마다
        # GET /, /panel.js, /ws 같은 접속 로그를 계속 남겨서 일부러 뺐다 —
        # 기기 연결 진단에 필요한 status/incoming/HTTPS/publish만 남긴다.
        "log": ["status", "incoming", "HTTPS", "publish"],
    }


def write_config(config_path: Path, values: RethinkSetupInput) -> None:
    config = build_config(values)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def validate(values: RethinkSetupInput) -> list[str]:
    """입력값 검증. 문제 목록을 반환 — 비어 있으면 통과."""
    errors: list[str] = []
    if not values.get("mqtt_host", "").strip():
        errors.append("MQTT 브로커 주소를 입력해주세요.")
    mqtt_port = values.get("mqtt_port", "").strip()
    if mqtt_port and not mqtt_port.isdigit():
        errors.append("MQTT 포트는 숫자여야 합니다.")
    return errors


def test_mqtt_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """host:port 로 TCP 연결이 되는지만 확인한다 (MQTT 핸드셰이크는 하지 않음)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"연결 성공: {host}:{port} 에 도달했습니다."
    except socket.gaierror:
        return False, f"주소를 찾을 수 없습니다: {host} (오타가 있는지 확인해주세요)."
    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        return False, f"연결 실패: {host}:{port} — {e}"
