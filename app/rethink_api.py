"""
rethink-cloud의 관리 API(management_port, 기본 44401)를 호출하는 아주 얇은 클라이언트.

지금 쓰는 건 bridge 비활성화 하나뿐이다:
  POST /bridge/:deviceId/disable

기기를 끄거나 지울 때 이걸 먼저 호출해서, 카페 글에서 확인된 "bridge를 안 끄고
리다이렉트부터 빼면 clientId 충돌로 재접속 루프에 빠진다" 문제를 자동으로
피하기 위함이다. deviceId는 devices.json에 사용자가 직접 채워 넣은 값(rethink
웹 UI의 "Connected devices" 표 ID 컬럼)에 의존한다 — rethink 관리 API 자체는
MAC 주소로 deviceId를 역으로 찾을 방법을 제공하지 않는다.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger("rethink_api")


def disable_bridge(management_port: int, device_id: str, timeout: float = 5.0) -> bool:
    """성공(204) 또는 이미 꺼져 있어 사실상 문제 없는 경우 True."""
    if not device_id:
        return False
    url = f"http://127.0.0.1:{management_port}/bridge/{device_id}/disable"
    try:
        resp = requests.post(url, timeout=timeout)
        if resp.status_code in (200, 204):
            logger.info("bridge 비활성화 성공: %s", device_id)
            return True
        logger.warning(
            "bridge 비활성화 실패 (%s): HTTP %s", device_id, resp.status_code
        )
        return False
    except requests.RequestException as e:
        logger.warning(
            "bridge 비활성화 요청 실패 (%s): %s (rethink-cloud가 실행 중이 아닐 수 있음)",
            device_id,
            e,
        )
        return False
