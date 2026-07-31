"""
최초 실행 시 rethink-config.json이 없거나 필수 값이 비어 있으면, 이 간단한
설정 웹페이지를 띄운다. 값을 입력받아 config를 만들고 나면 콜백으로
rethink-cloud를 시작시킨다.

"연결 테스트" 버튼은 MQTT 브로커 주소:포트에 TCP 연결이 되는지만 확인한다
(실제 MQTT CONNECT 핸드셰이크까지는 하지 않음 — 그래도 잘못된 IP/포트,
브로커가 꺼져 있음, 방화벽 차단 같은 흔한 실수는 이걸로 다 걸러진다).

외부 의존성 없이 파이썬 표준 라이브러리(http.server, socket)만 사용한다.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import rethink_config

logger = logging.getLogger("setup_server")

_FORM_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>LG Local Manager - 최초 설정</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 480px;
         margin: 60px auto; padding: 0 20px; color: #222; }}
  h1 {{ font-size: 20px; }}
  p.desc {{ color: #555; line-height: 1.5; }}
  label {{ display: block; margin-top: 16px; font-weight: 600; }}
  input {{ width: 100%; box-sizing: border-box; padding: 8px; margin-top: 4px;
           font-size: 14px; border: 1px solid #ccc; border-radius: 4px; }}
  button {{ padding: 10px 20px; font-size: 15px; border: none; border-radius: 4px;
            cursor: pointer; }}
  button.primary {{ margin-top: 24px; background: #1e88e5; color: white; }}
  button.secondary {{ margin-top: 8px; background: #eee; color: #333; }}
  button:disabled {{ opacity: 0.6; cursor: default; }}
  .error {{ background: #fdecea; color: #b3261e; padding: 10px; border-radius: 4px;
            margin-top: 16px; }}
  .hint {{ color: #888; font-size: 12px; margin-top: 2px; }}
  #test-result {{ margin-top: 8px; font-size: 13px; min-height: 18px; }}
  #test-result.ok {{ color: #1e7e34; }}
  #test-result.fail {{ color: #b3261e; }}
</style>
</head>
<body>
  <h1>LG Local Manager 최초 설정</h1>
  <p class="desc">
    rethink-cloud를 시작하기 전에 MQTT 브로커 정보가 필요합니다.
    이미 운영 중인 브로커(예: Home Assistant의 Mosquitto)의 주소를 입력해주세요.
  </p>
  {error_html}
  <form method="POST" action="/" id="setup-form">
    <label>MQTT 브로커 주소 (필수)</label>
    <input type="text" name="mqtt_host" id="mqtt_host" placeholder="192.168.0.50" value="{mqtt_host}" required>
    <div class="hint">호스트명 또는 IP만 입력 (mqtt:// 붙이지 않음)</div>

    <label>MQTT 포트</label>
    <input type="text" name="mqtt_port" id="mqtt_port" placeholder="1883" value="{mqtt_port}">

    <button type="button" class="secondary" id="test-btn">연결 테스트</button>
    <div id="test-result"></div>

    <label>MQTT 사용자 이름 (선택)</label>
    <input type="text" name="mqtt_user" value="{mqtt_user}">

    <label>MQTT 비밀번호 (선택)</label>
    <input type="password" name="mqtt_pass" value="{mqtt_pass}">

    <label>rethink 호스트 이름 (선택, 기본값 권장)</label>
    <input type="text" name="hostname" placeholder="rethink.lan" value="{hostname}">

    <button type="submit" class="primary">설정 완료</button>
  </form>

  <script>
    document.getElementById('test-btn').addEventListener('click', async () => {{
      const btn = document.getElementById('test-btn');
      const result = document.getElementById('test-result');
      const host = document.getElementById('mqtt_host').value.trim();
      const port = document.getElementById('mqtt_port').value.trim() || '1883';

      if (!host) {{
        result.className = 'fail';
        result.textContent = '먼저 MQTT 브로커 주소를 입력해주세요.';
        return;
      }}

      btn.disabled = true;
      result.className = '';
      result.textContent = '연결 확인 중...';

      try {{
        const resp = await fetch('/test', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
          body: new URLSearchParams({{mqtt_host: host, mqtt_port: port}}),
        }});
        const data = await resp.json();
        result.className = data.ok ? 'ok' : 'fail';
        result.textContent = data.message;
      }} catch (e) {{
        result.className = 'fail';
        result.textContent = '테스트 요청 자체가 실패했습니다: ' + e;
      }} finally {{
        btn.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""

_SUCCESS_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="3;url=http://127.0.0.1:{management_port}/">
<title>설정 완료</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 480px;
         margin: 100px auto; padding: 0 20px; color: #222; text-align: center; }}
</style>
</head>
<body>
  <h1>설정 완료!</h1>
  <p>rethink-cloud를 시작합니다. 잠시 후 관리 화면으로 이동합니다...</p>
  <p><a href="http://127.0.0.1:{management_port}/">지금 바로 이동</a></p>
</body>
</html>
"""


def test_mqtt_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """host:port 로 TCP 연결이 되는지만 확인한다 (MQTT 핸드셰이크는 하지 않음)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"연결 성공: {host}:{port} 에 도달했습니다."
    except socket.gaierror:
        return False, f"주소를 찾을 수 없습니다: {host} (오타가 있는지 확인해주세요)."
    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        return False, f"연결 실패: {host}:{port} — {e}"


class SetupServer:
    def __init__(
        self,
        config_path: Path,
        management_port: int,
        on_configured,
        bind_port: int = 44490,
    ):
        self.config_path = config_path
        self.management_port = management_port
        self.on_configured = on_configured
        self.bind_port = bind_port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def url(self) -> str:
        return f"http://127.0.0.1:{self.bind_port}/"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        handler = _make_handler(self.config_path, self.management_port, self._on_done)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.bind_port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="setup-server"
        )
        self._thread.start()
        logger.info("최초 설정 서버 시작: %s", self.url())

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        logger.info("최초 설정 서버 중단")

    def _on_done(self) -> None:
        # 완료 페이지가 응답을 마친 뒤 서버를 내리고 rethink를 시작시킨다.
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self) -> None:
        import time

        time.sleep(0.5)
        self.stop()
        self.on_configured()


def _make_handler(config_path: Path, management_port: int, on_done):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            logger.info("[setup-http] " + fmt, *args)

        def _render_form(self, error: str = "", values: dict | None = None) -> None:
            values = values or {}
            error_html = f'<div class="error">{error}</div>' if error else ""
            body = _FORM_PAGE.format(
                error_html=error_html,
                mqtt_host=values.get("mqtt_host", ""),
                mqtt_port=values.get("mqtt_port", "1883"),
                mqtt_user=values.get("mqtt_user", ""),
                mqtt_pass=values.get("mqtt_pass", ""),
                hostname=values.get("hostname", ""),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._render_form()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8")
            parsed = {k: v[0] for k, v in parse_qs(raw).items()}

            if self.path == "/test":
                host = parsed.get("mqtt_host", "").strip()
                port_raw = parsed.get("mqtt_port", "").strip() or "1883"
                if not host:
                    self._send_json(400, {"ok": False, "message": "MQTT 브로커 주소를 입력해주세요."})
                    return
                if not port_raw.isdigit():
                    self._send_json(400, {"ok": False, "message": "포트는 숫자여야 합니다."})
                    return
                ok, message = test_mqtt_reachable(host, int(port_raw))
                self._send_json(200, {"ok": ok, "message": message})
                return

            # 그 외(POST /) -> 설정 저장
            errors = rethink_config.validate(parsed)
            if errors:
                self._render_form(error=" / ".join(errors), values=parsed)
                return

            rethink_config.write_config(config_path, parsed)  # type: ignore[arg-type]
            body = _SUCCESS_PAGE.format(management_port=management_port).encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            on_done()

    return Handler
