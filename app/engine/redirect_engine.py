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

# LG 기기가 실제로 접속을 시도하는 목적지 포트(기기 펌웨어에 고정된 값이라
# 우리가 바꿀 수 없다). 이 포트로 나가는 패킷만 가로채서 로컬 rethink 포트로
# 목적지를 재작성한다.
LG_HTTPS_PORT = 443
LG_MQTTS_PORT = 8883


class RedirectWorker:
    """기기 하나(IP 기준)를 대상으로 443/8883 목적지를 재작성하는 WinDivert 워커."""

    def __init__(
        self,
        device_ip: str,
        rethink_host: str,
        https_port: int,
        mqtts_port: int,
    ):
        self.device_ip = device_ip
        self.rethink_host = rethink_host
        self.https_port = https_port
        self.mqtts_port = mqtts_port
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None

    def _filter(self) -> str:
        # 진단을 위해 일부러 방향(inbound/outbound) 제한을 뺐다 — ARP 스푸핑이
        # 실제로 패킷을 이 PC로 끌어오고 있는지, 방향이 우리가 가정한 대로인지
        # 자체가 아직 확인 안 됐기 때문이다. _run()에서 잡히는 패킷마다 방향을
        # 로그로 남기고, 그 결과를 보고 나중에 정확한 방향으로 좁힌다.
        return (
            f"ip.SrcAddr == {self.device_ip} and tcp and "
            f"(tcp.DstPort == {LG_HTTPS_PORT} or tcp.DstPort == {LG_MQTTS_PORT})"
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
                logger.info(
                    "WinDivert 필터 활성화 (%s): %s", self.device_ip, self._filter()
                )
                packet_count = 0
                for packet in w:
                    if self._stop_event.is_set():
                        break
                    packet_count += 1
                    original_dst = packet.dst_addr
                    original_port = packet.tcp.dst_port
                    if packet.tcp.dst_port == LG_HTTPS_PORT:
                        packet.dst_addr = self.rethink_host
                        packet.tcp.dst_port = self.https_port
                    elif packet.tcp.dst_port == LG_MQTTS_PORT:
                        packet.dst_addr = self.rethink_host
                        packet.tcp.dst_port = self.mqtts_port
                    # 처음 20개는 상세히 남긴다 — 이걸로 (1) ARP 스푸핑이 실제로
                    # 패킷을 끌어오고 있는지, (2) 실측 방향(is_outbound)이
                    # 뭔지, (3) 재작성이 실제로 일어났는지를 확인한다. 그 이후는
                    # 로그가 넘치지 않게 조용히 처리만 한다.
                    if packet_count <= 20:
                        logger.info(
                            "패킷 캡처 #%d (%s): outbound=%s %s:%s -> %s:%s "
                            "(재작성 후 -> %s:%s)",
                            packet_count,
                            self.device_ip,
                            packet.is_outbound,
                            packet.src_addr,
                            packet.tcp.src_port,
                            original_dst,
                            original_port,
                            packet.dst_addr,
                            packet.tcp.dst_port,
                        )
                    w.send(packet)
        except Exception as e:  # noqa: BLE001
            if self._stop_event.is_set():
                # stop()이 블로킹 중인 for-loop을 깨우려고 일부러 핸들을
                # 닫아서 생기는 예외다 — 종료 과정의 정상적인 일부라
                # ERROR가 아니라 INFO로 남긴다.
                logger.info(
                    "리다이렉트 워커 종료 (%s): 정상 종료에 따른 handle 닫힘 (%s)",
                    self.device_ip,
                    e,
                )
            else:
                logger.error("리다이렉트 워커 오류 (%s): %s", self.device_ip, e)


class RedirectEngine:
    """여러 기기의 RedirectWorker를 devices.json 상태와 동기화한다."""

    def __init__(self, rethink_host: str, https_port: int, mqtts_port: int):
        self.rethink_host = rethink_host
        self.https_port = https_port
        self.mqtts_port = mqtts_port
        self._workers: dict[str, RedirectWorker] = {}

    def sync(self, device_ips: list[str]) -> None:
        wanted = set(device_ips)

        for ip in list(self._workers.keys()):
            if ip not in wanted:
                self._workers.pop(ip).stop()

        for ip in wanted:
            if ip not in self._workers:
                worker = RedirectWorker(
                    ip, self.rethink_host, self.https_port, self.mqtts_port
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
