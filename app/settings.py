"""
config/settings.json 로드/저장.

main.py가 직접 JSON을 다루던 걸 분리했다 — main.py는 "설정값이 필요하다"만
알면 되고, 파일 형식/기본값/오류 처리는 여기서 책임진다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("settings")

DEFAULT_SETTINGS = {
    "gateway_ip": "192.168.0.1",
    "rethink_host": "127.0.0.1",
    "update_channel": "stable",  # "stable" | "beta"
}


class AppSettings:
    def __init__(self, path: Path, values: dict):
        self.path = path
        self.values = values

    @classmethod
    def load(cls, path: Path) -> "AppSettings":
        values = dict(DEFAULT_SETTINGS)
        if not path.exists():
            logger.warning("%s 이 없어 기본값을 사용합니다: %s", path, values)
            return cls(path, values)

        try:
            values.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("%s 를 읽는 중 오류가 나서 기본값을 사용합니다: %s", path, e)
        return cls(path, values)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, key: str) -> str:
        return self.values[key]
