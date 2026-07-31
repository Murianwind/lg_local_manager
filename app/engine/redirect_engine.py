"""
WinDivert(pydivert)를 이용해 등록된 기기의 443/8883 트래픽 목적지를
로컬 rethink 서버로 재작성한다 (Linux iptables DNAT과 동일한 역할).

카페 글 기준으로 확인된 원칙을 그대로 따른다:
- DNS는 건드리지 않는다. 기기는 여전히 LG의 진짜 IP를 그대로 캐시하고,
  목적지 재작성 여부만으로 on/off가 결정되게 한다.
- 규칙을 켜고 끄는 것만으로 rethink <-> LG 공식 서버 전환이 가능해야 한다.
- 되돌릴 때는 (1) rethink 쪽 bridge를 먼저 끄고 (2) 이 리다이렉트 규칙을
  제거하는 순서를 지켜야 한다. clientId 충돌로 재접속 루프에 빠지는 걸
  막기 위함이며, 이 순서 강제는 상위 오케스트레이터(orchestrator.py)가 담당한다.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("redirect_engine")

try:
    import pydivert  # type: ignore
except ImportError:
    pydivert = None  # WinDivert 미설치 환경에서도 모듈 자체는 import 가능하게


class RedirectWorker:
    """기기 하나(IP 기준)를 대상으로 443/8883 목적지를 재작성하는 WinDivert 워커."""

    def __init__(
        self,
        device_ip: str,
        rethink_host: str,
        https_port: int,
        mqtt_port: int,
    ):
        self.device_ip = device_ip
        self.rethink_host = rethink_host
        self.https_port = https_port
        self.mqtt_port = mqtt_port
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None

    def _filter(self) -> str:
        # 이 기기가 보내는 443/8883번 목적지 패킷만 가로챈다.
        return (
            f"ip.SrcAddr == {self.device_ip} and "
            f"tcp and (tcp.DstPort == 443 or tcp.DstPort == 8883) and outbound"
        )

    def start(self) -> None:
        if pydivert is None:
            logger.error("pydivert(WinDivert) 미설치: 리다이렉트를 실행할 수 없습니다.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"redirect-{self.device_ip}"
        )
        self._thread.start()
        logger.info("리다이렉트 시작: %s -> %s", self.device_ip, self.rethink_host)

    def stop(self) -> None:
        self._stop_event.set()
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("리다이렉트 중단: %s", self.device_ip)

    def _run(self) -> None:
        try:
            with pydivert.WinDivert(self._filter()) as w:
                self._handle = w
                for packet in w:
                    if self._stop_event.is_set():
                        break
                    if packet.tcp.dst_port == 443:
                        packet.dst_addr = self.rethink_host
                        packet.tcp.dst_port = self.https_port
                    elif packet.tcp.dst_port == 8883:
                        packet.dst_addr = self.rethink_host
                        packet.tcp.dst_port = self.mqtt_port
                    w.send(packet)
        except Exception as e:  # noqa: BLE001
            logger.error("리다이렉트 워커 오류 (%s): %s", self.device_ip, e)


class RedirectEngine:
    """여러 기기의 RedirectWorker를 devices.json 상태와 동기화한다."""

    def __init__(self, rethink_host: str, https_port: int, mqtt_port: int):
        self.rethink_host = rethink_host
        self.https_port = https_port
        self.mqtt_port = mqtt_port
        self._workers: dict[str, RedirectWorker] = {}

    def sync(self, device_ips: list[str]) -> None:
        wanted = set(device_ips)

        for ip in list(self._workers.keys()):
            if ip not in wanted:
                self._workers.pop(ip).stop()

        for ip in wanted:
            if ip not in self._workers:
                worker = RedirectWorker(
                    ip, self.rethink_host, self.https_port, self.mqtt_port
                )
                self._workers[ip] = worker
                worker.start()

    def stop_device(self, ip: str) -> None:
        """되돌리기: bridge를 먼저 끈 뒤 오케스트레이터가 이걸 호출해야 한다."""
        if ip in self._workers:
            self._workers.pop(ip).stop()

    def stop_all(self) -> None:
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()
