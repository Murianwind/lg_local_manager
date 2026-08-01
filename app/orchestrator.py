"""
devices.json 상태 <-> ARP 스푸핑 <-> 리다이렉트 <-> rethink 프로세스를
하나로 묶는 오케스트레이터.

지켜야 하는 순서 (카페 글 및 rethink PR 논의에서 확인된 원칙):
  켤 때:   ARP 스푸핑 시작 -> 리다이렉트(DNAT) 시작 -> (기기가 재접속하면)
           rethink 웹 UI에서 사용자가 bridge 활성화
  끌 때:   rethink 쪽 bridge 비활성화 -> 리다이렉트 제거 -> ARP 스푸핑 중단/복구
           (순서를 반대로 하면 clientId 충돌로 재접속 루프에 빠진다)

bridge 비활성화는 devices.json에 rethink_device_id가 채워져 있으면 rethink의
관리 API(POST /bridge/:deviceId/disable)를 직접 호출해 자동으로 처리한다.
값이 없으면(아직 rethink 웹 UI에서 ID를 확인해 넣지 않은 경우) 예전처럼
"먼저 꺼주세요" 안내만 띄우는 걸로 안전하게 폴백한다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import rethink_api
from .device_store import Device, DeviceStore
from .engine.arp_engine import ArpEngine, ArpSpoofTarget
from .engine.redirect_engine import RedirectEngine
from .engine.rethink_process import RethinkProcess
from .version import APP_NAME

logger = logging.getLogger("orchestrator")

# rethink-cloud 자신의 stdout/stderr를 우리 로그 파일에 그대로 옮겨 적는데(관리
# 편의를 위해), 혹시라도 rethink가 자기 config를 로그에 찍는 경우를 대비해
# MQTT 계정 정보는 옮겨 적기 전에 마스킹한다.
_LOG_REDACTED = "***"


class Orchestrator:
    def __init__(
        self,
        store: DeviceStore,
        gateway_ip: str,
        rethink_host: str,
        runtime_dir: Path,
        config_dir: Path,
        notify=None,
    ):
        self.store = store
        self.notify = notify or (lambda title, msg: None)
        self._rethink_config_path = config_dir / "rethink-config.json"
        self._log_redact_terms: list[str] = []

        self.arp_engine = ArpEngine(
            gateway_ip=gateway_ip,
            on_error=lambda msg: self.notify(APP_NAME, msg),
        )
        self.redirect_engine = RedirectEngine(
            rethink_host=rethink_host,
            https_port=store.rethink_ports["https_port"],
            mqtts_port=store.rethink_ports["mqtts_port"],
        )
        self.rethink_process = RethinkProcess(
            node_exe=runtime_dir / "node" / "node.exe",
            rethink_entry=runtime_dir / "rethink" / "dist" / "rethink-cloud.js",
            app_dir=runtime_dir.parent,
            config_path=self._rethink_config_path,
            log_callback=self._on_rethink_log,
        )

    def _load_log_redact_terms(self) -> list[str]:
        """rethink-config.json에서 마스킹할 값(MQTT 계정 정보)을 읽어온다.

        설정이 아직 없을 수도 있고(최초 실행 전), 값이 비어 있을 수도 있으니
        실패해도 조용히 빈 목록을 돌려준다 — 로그 마스킹은 방어적 조치일 뿐,
        여기서 실패했다고 rethink 기동을 막을 이유는 없다.
        """
        try:
            config = json.loads(self._rethink_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        ha_config = config.get("homeassistant", {})
        return [
            value
            for value in (ha_config.get("mqtt_user"), ha_config.get("mqtt_pass"))
            if value
        ]

    def _on_rethink_log(self, line: str) -> None:
        for secret in self._log_redact_terms:
            line = line.replace(secret, _LOG_REDACTED)
        logger.info("[rethink] %s", line)

    def start(self) -> None:
        self._log_redact_terms = self._load_log_redact_terms()
        self.rethink_process.start()
        self._sync_devices()

    def stop(self) -> None:
        """전체 종료: 반드시 bridge -> 리다이렉트/ARP -> rethink 순으로 내린다."""
        self._disable_bridge_for_all_enabled()
        self.arp_engine.stop_all()
        self.redirect_engine.stop_all()
        self.rethink_process.stop()

    def _sync_devices(self) -> None:
        enabled = self.store.enabled_devices()
        targets = [ArpSpoofTarget(d.name, d.ip, d.mac) for d in enabled]
        self.arp_engine.sync(targets)
        self.redirect_engine.sync([d.ip for d in enabled])

    def on_devices_changed(self) -> None:
        logger.info("devices.json 변경 감지 -> 엔진 재동기화")
        self._sync_devices()

    def add_device(self, name: str, mac: str, ip: str, rethink_device_id: str = "") -> None:
        self.store.add_device(
            Device(name=name, mac=mac, ip=ip, enabled=True, rethink_device_id=rethink_device_id)
        )
        self._sync_devices()

    def _disable_bridge(self, device: Device) -> None:
        """가능하면 자동으로, 아니면 안내 알림으로 폴백."""
        if not device.rethink_device_id:
            self.notify(
                APP_NAME,
                f"{device.name}은 rethink Device ID가 등록되어 있지 않습니다 — "
                "rethink 웹 UI에서 이 기기의 bridge를 먼저 꺼주세요.",
            )
            return

        management_port = self.store.rethink_ports["management_port"]
        if rethink_api.disable_bridge(management_port, device.rethink_device_id):
            self.notify(APP_NAME, f"{device.name}의 bridge를 자동으로 껐습니다.")
        else:
            self.notify(
                APP_NAME,
                f"{device.name}의 bridge 자동 비활성화에 실패했습니다 — "
                "rethink 웹 UI에서 직접 꺼주세요.",
            )

    def _disable_bridge_for_all_enabled(self) -> None:
        for device in self.store.enabled_devices():
            self._disable_bridge(device)

    def remove_device(self, mac: str) -> None:
        """삭제 = 원상복귀. bridge부터 끈 뒤, 리다이렉트 -> ARP 스푸핑 순서로 내린다."""
        device = self.store.find(mac)
        if device:
            self._disable_bridge(device)
            self.redirect_engine.stop_device(device.ip)
        self.store.remove_device(mac)
        self._sync_devices()

    def set_device_enabled(self, mac: str, enabled: bool) -> None:
        if not enabled:
            device = self.store.find(mac)
            if device:
                self._disable_bridge(device)
        self.store.set_enabled(mac, enabled)
        self._sync_devices()
