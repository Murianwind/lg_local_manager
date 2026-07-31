"""
devices.json 상태 <-> ARP 스푸핑 <-> 리다이렉트 <-> rethink 프로세스를
하나로 묶는 오케스트레이터.

지켜야 하는 순서 (카페 글 및 rethink PR 논의에서 확인된 원칙):
  켤 때:   ARP 스푸핑 시작 -> 리다이렉트(DNAT) 시작 -> (기기가 재접속하면)
           rethink 웹 UI에서 사용자가 bridge 활성화
  끌 때:   rethink 쪽 bridge 비활성화 -> 리다이렉트 제거 -> ARP 스푸핑 중단/복구
           (순서를 반대로 하면 clientId 충돌로 재접속 루프에 빠진다)

bridge on/off 자체는 rethink의 관리 웹 UI(44401 포트)에서 사용자가 직접
누르는 동작이라 이 앱이 대신 눌러주지 않는다. 대신 "지금 리다이렉트를 끄기
전에 bridge부터 끄세요" 라는 안내를 트레이 알림으로 띄워준다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .device_store import Device, DeviceStore
from .engine.arp_engine import ArpEngine, ArpSpoofTarget
from .engine.redirect_engine import RedirectEngine
from .engine.rethink_process import RethinkProcess

logger = logging.getLogger("orchestrator")


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

        self.arp_engine = ArpEngine(gateway_ip=gateway_ip)
        self.redirect_engine = RedirectEngine(
            rethink_host=rethink_host,
            https_port=store.rethink_ports["https_port"],
            mqtt_port=store.rethink_ports["mqtt_port"],
        )
        self.rethink_process = RethinkProcess(
            node_exe=runtime_dir / "node" / "node.exe",
            rethink_entry=runtime_dir / "rethink" / "dist" / "rethink-cloud.js",
            config_path=config_dir / "rethink-config.json",
            log_callback=self._on_rethink_log,
        )

    def _on_rethink_log(self, line: str) -> None:
        logger.info("[rethink] %s", line)

    def start(self) -> None:
        self.rethink_process.start()
        self._sync_devices()

    def stop(self) -> None:
        """전체 종료: 반드시 리다이렉트/ARP부터 내린 뒤 rethink를 내린다."""
        self.notify(
            "LG Local Manager",
            "종료 전에 rethink 웹 UI에서 bridge를 먼저 꺼주세요 (기기별로).",
        )
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

    def add_device(self, name: str, mac: str, ip: str) -> None:
        self.store.add_device(Device(name=name, mac=mac, ip=ip, enabled=True))
        self._sync_devices()

    def remove_device(self, mac: str) -> None:
        """
        삭제 = 원상복귀. bridge를 먼저 끄라고 안내한 뒤,
        리다이렉트 -> ARP 스푸핑 순서로 내린다.
        """
        self.notify(
            "LG Local Manager",
            "삭제하기 전에 rethink 웹 UI에서 이 기기의 bridge를 먼저 꺼주세요.",
        )
        device = next((d for d in self.store.devices if d.mac.upper() == mac.upper()), None)
        if device:
            self.redirect_engine.stop_device(device.ip)
        self.store.remove_device(mac)
        self._sync_devices()

    def set_device_enabled(self, mac: str, enabled: bool) -> None:
        if not enabled:
            self.notify(
                "LG Local Manager",
                "끄기 전에 rethink 웹 UI에서 이 기기의 bridge를 먼저 꺼주세요.",
            )
        self.store.set_enabled(mac, enabled)
        self._sync_devices()
