"""
등록된 기기에 한해서만 ARP 스푸핑을 수행한다.

동작 원리:
- 대상 기기에게는 "내가 게이트웨이다"라고 알리는 ARP reply를 주기적으로 보낸다.
- 게이트웨이에게는 "내가 그 기기다"라고 알리는 ARP reply를 주기적으로 보낸다.
  (양방향 스푸핑을 해야 응답 트래픽도 우리 쪽을 거쳐서 실제로 전달할 수 있다.
   대상 기기 -> 우리 -> 실제 목적지로 라우팅해줘야 하므로 IP 포워딩도 활성화한다.)
- Windows에는 raw L2 접근을 위해 Npcap 드라이버가 필요하다 (scapy가 그걸 사용).

주의: 이 모듈은 자신이 관리자로서 소유/운영하는 자기 집 네트워크에서,
자신이 등록한 자신의 기기에 대해서만 사용하도록 설계되어 있다.
devices.json에 등록되지 않은 기기는 절대 대상이 되지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("arp_engine")

try:
    from scapy.all import ARP, Ether, conf, get_if_hwaddr, send, srp  # type: ignore
except ImportError:  # scapy/Npcap 미설치 환경에서도 이 모듈만은 import 가능하게
    ARP = Ether = conf = None  # type: ignore

    def get_if_hwaddr(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")

    def send(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")

    def srp(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")


@dataclass
class ArpSpoofTarget:
    name: str
    ip: str
    mac: str


class ArpSpoofWorker:
    """기기 하나를 대상으로 하는 지속적 ARP 스푸핑 스레드."""

    def __init__(self, target: ArpSpoofTarget, gateway_ip: str, interval_sec: float = 2.0):
        self.target = target
        self.gateway_ip = gateway_ip
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._my_mac: str | None = None
        self._gateway_mac: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"arp-{self.target.name}"
        )
        self._thread.start()
        logger.info("ARP 스푸핑 시작: %s (%s)", self.target.name, self.target.ip)

    def stop(self, restore: bool = True) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if restore:
            self._restore()
        logger.info("ARP 스푸핑 중단: %s (%s)", self.target.name, self.target.ip)

    def _resolve_mac(self, ip: str, retries: int = 3) -> str | None:
        for _ in range(retries):
            ans, _ = srp(
                Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=ip),
                timeout=2,
                verbose=False,
            )
            if ans:
                return ans[0][1].hwsrc
        return None

    def _run(self) -> None:
        if conf is None:
            logger.error("scapy 미설치: ARP 스푸핑을 실행할 수 없습니다.")
            return

        self._my_mac = get_if_hwaddr(conf.iface)
        self._gateway_mac = self._resolve_mac(self.gateway_ip)
        if not self._gateway_mac:
            logger.error("게이트웨이(%s) MAC을 확인하지 못했습니다.", self.gateway_ip)
            return

        while not self._stop_event.is_set():
            try:
                # 대상 기기에게: "게이트웨이 IP는 나(우리 PC)의 MAC이다"
                send(
                    ARP(
                        op=2,
                        pdst=self.target.ip,
                        hwdst=self.target.mac,
                        psrc=self.gateway_ip,
                        hwsrc=self._my_mac,
                    ),
                    verbose=False,
                )
                # 게이트웨이에게: "대상 기기 IP는 나(우리 PC)의 MAC이다"
                send(
                    ARP(
                        op=2,
                        pdst=self.gateway_ip,
                        hwdst=self._gateway_mac,
                        psrc=self.target.ip,
                        hwsrc=self._my_mac,
                    ),
                    verbose=False,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("ARP 패킷 전송 실패 (%s): %s", self.target.name, e)
            self._stop_event.wait(self.interval_sec)

    def _restore(self) -> None:
        """정상적인 ARP 매핑으로 되돌린다 (스푸핑 이전 상태 복구)."""
        if conf is None or not self._gateway_mac:
            return
        try:
            for _ in range(5):
                send(
                    ARP(
                        op=2,
                        pdst=self.target.ip,
                        hwdst=self.target.mac,
                        psrc=self.gateway_ip,
                        hwsrc=self._gateway_mac,
                    ),
                    verbose=False,
                )
                send(
                    ARP(
                        op=2,
                        pdst=self.gateway_ip,
                        hwdst=self._gateway_mac,
                        psrc=self.target.ip,
                        hwsrc=self.target.mac,
                    ),
                    verbose=False,
                )
                time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            logger.warning("ARP 복구 실패 (%s): %s", self.target.name, e)


class ArpEngine:
    """여러 기기의 ArpSpoofWorker를 관리한다."""

    def __init__(self, gateway_ip: str):
        self.gateway_ip = gateway_ip
        self._workers: dict[str, ArpSpoofWorker] = {}

    def sync(self, targets: list[ArpSpoofTarget]) -> None:
        """devices.json의 enabled 목록과 현재 실행 중인 워커를 맞춘다."""
        wanted = {t.mac: t for t in targets}

        # enabled 해제되었거나 삭제된 기기 -> 중단
        for mac in list(self._workers.keys()):
            if mac not in wanted:
                self._workers.pop(mac).stop(restore=True)

        # 새로 enabled 된 기기 -> 시작
        for mac, target in wanted.items():
            if mac not in self._workers:
                worker = ArpSpoofWorker(target, self.gateway_ip)
                self._workers[mac] = worker
                worker.start()

    def stop_all(self) -> None:
        for worker in self._workers.values():
            worker.stop(restore=True)
        self._workers.clear()
