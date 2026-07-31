"""
devices.json 을 읽고 쓰는 저장소.

파일 하나로 "등록된 기기 목록"을 관리한다. 트레이 앱 GUI에서 추가/삭제하거나,
파일을 직접 텍스트 에디터로 열어서 편집해도 된다 (앱이 변경을 감지해서 반영함).

스키마 예시:
{
  "rethink": {
    "https_port": 4433,
    "mqtt_port": 8883,
    "mgmt_port": 44401
  },
  "devices": [
    {
      "name": "거실 에어컨",
      "mac": "AA:BB:CC:11:22:33",
      "ip": "192.168.0.101",
      "enabled": true
    }
  ]
}
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

DEFAULT_RETHINK_PORTS = {
    "https_port": 4433,
    "mqtt_port": 8883,
    "mgmt_port": 44401,
}


class DeviceValidationError(ValueError):
    pass


@dataclass
class Device:
    name: str
    mac: str
    ip: str
    enabled: bool = True

    def validate(self) -> None:
        if not self.name.strip():
            raise DeviceValidationError("기기 이름이 비어 있습니다.")
        if not MAC_RE.match(self.mac):
            raise DeviceValidationError(f"MAC 주소 형식이 올바르지 않습니다: {self.mac}")
        if not IPV4_RE.match(self.ip):
            raise DeviceValidationError(f"IP 주소 형식이 올바르지 않습니다: {self.ip}")

    def normalized(self) -> "Device":
        return Device(
            name=self.name.strip(),
            mac=self.mac.upper(),
            ip=self.ip.strip(),
            enabled=bool(self.enabled),
        )


@dataclass
class DeviceStore:
    """devices.json 을 감싸는 스레드-세이프 저장소."""

    path: Path
    rethink_ports: dict = field(default_factory=lambda: dict(DEFAULT_RETHINK_PORTS))
    devices: list[Device] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _mtime: float = field(default=0.0, repr=False)

    @classmethod
    def load(cls, path: Path) -> "DeviceStore":
        store = cls(path=path)
        if path.exists():
            store._load_from_disk()
        else:
            store.save()  # 최초 실행 시 빈 파일 생성
        return store

    def _load_from_disk(self) -> None:
        with self._lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.rethink_ports = {**DEFAULT_RETHINK_PORTS, **raw.get("rethink", {})}
            devices = []
            for i, d in enumerate(raw.get("devices", [])):
                try:
                    dev = Device(**d).normalized()
                    dev.validate()
                    devices.append(dev)
                except (TypeError, DeviceValidationError) as e:
                    print(f"[devices.json] {i}번째 기기 항목을 건너뜁니다: {e}")
            self.devices = devices
            self._mtime = self.path.stat().st_mtime

    def reload_if_changed(self) -> bool:
        """파일이 외부에서 수정됐으면 다시 읽는다. 변경이 있었으면 True."""
        with self._lock:
            if not self.path.exists():
                return False
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._load_from_disk()
                return True
            return False

    def save(self) -> None:
        with self._lock:
            payload = {
                "rethink": self.rethink_ports,
                "devices": [asdict(d) for d in self.devices],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
            self._mtime = self.path.stat().st_mtime

    def add_device(self, device: Device) -> None:
        device = device.normalized()
        device.validate()
        with self._lock:
            for existing in self.devices:
                if existing.mac == device.mac:
                    raise DeviceValidationError(f"이미 등록된 MAC 입니다: {device.mac}")
            self.devices.append(device)
            self.save()

    def remove_device(self, mac: str) -> None:
        mac = mac.upper()
        with self._lock:
            before = len(self.devices)
            self.devices = [d for d in self.devices if d.mac != mac]
            if len(self.devices) == before:
                raise DeviceValidationError(f"등록되지 않은 MAC 입니다: {mac}")
            self.save()

    def set_enabled(self, mac: str, enabled: bool) -> None:
        mac = mac.upper()
        with self._lock:
            for d in self.devices:
                if d.mac == mac:
                    d.enabled = enabled
                    self.save()
                    return
            raise DeviceValidationError(f"등록되지 않은 MAC 입니다: {mac}")

    def enabled_devices(self) -> list[Device]:
        with self._lock:
            return [d for d in self.devices if d.enabled]


def watch(store: DeviceStore, on_change: Callable[[], None], interval_sec: float = 2.0):
    """별도 스레드에서 devices.json 변경을 폴링한다."""
    import time

    def _loop():
        while True:
            try:
                if store.reload_if_changed():
                    on_change()
            except Exception as e:  # noqa: BLE001
                print(f"[watch] devices.json 감시 중 오류: {e}")
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, daemon=True, name="devices-watch")
    t.start()
    return t
