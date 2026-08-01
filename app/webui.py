"""
LG Local Manager 통합 웹 UI.

트레이 아이콘을 (왼쪽) 클릭하면 이 서버(기본 http://127.0.0.1:44490/)가 뜬다.
상태에 따라 같은 주소, 같은 서버가 다르게 그린다:
  - rethink-config.json이 아직 안 채워졌으면 -> 최초 설정 폼
  - 채워졌으면 -> 기기 관리/업데이트/시작 옵션을 다루는 대시보드

기기 등록/삭제, 업데이트 확인/설치, 자동 시작 토글처럼 예전엔 트레이
컨텍스트 메뉴 + Tkinter 창으로 흩어져 있던 조작을 전부 이 페이지 하나로
모았다. 트레이 메뉴에는 이제 "대시보드 열기"와 "종료"만 남는다.

외부 의존성 없이 파이썬 표준 라이브러리(http.server)만 사용한다.
폼 제출은 전부 POST -> 303 리다이렉트 -> GET / 패턴이라, 자바스크립트 없이도
동작하고 새로고침해도 중복 제출이 안 된다.
"""

from __future__ import annotations

import html
import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from . import rethink_config, updater
from .device_store import Device, DeviceStore, DeviceValidationError
from .orchestrator import Orchestrator
from .updater import UpdateInfo
from .version import APP_NAME

logger = logging.getLogger("webui")

_STYLE = """
body { font-family: -apple-system, "Segoe UI", sans-serif; max-width: 760px;
       margin: 40px auto; padding: 0 20px; color: #222; }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 16px; color: #1e88e5; border-bottom: 1px solid #eee;
     padding-bottom: 6px; margin-top: 36px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 12px; background: #eef; color: #335; margin-left: 6px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee;
         font-size: 13px; }
th { color: #666; font-weight: 600; }
input[type=text], input[type=password] { padding: 6px; border: 1px solid #ccc;
       border-radius: 4px; font-size: 13px; }
button { padding: 6px 14px; font-size: 13px; border: none; border-radius: 4px;
         cursor: pointer; background: #1e88e5; color: white; }
button.secondary { background: #eee; color: #333; }
button.danger { background: #e53935; color: white; }
form.inline { display: inline; }
.row { display: flex; gap: 8px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.hint { color: #888; font-size: 12px; }
.error { background: #fdecea; color: #b3261e; padding: 10px; border-radius: 4px;
         margin-top: 12px; }
.notice { background: #e8f5e9; color: #1e7e34; padding: 10px; border-radius: 4px;
          margin-top: 12px; }
pre.log { background: #1e1e1e; color: #ddd; padding: 10px; border-radius: 4px;
          max-height: 260px; overflow-y: auto; font-size: 11px; }
.link-btn { display: inline-block; padding: 6px 14px; background: #1e88e5;
            color: white; border-radius: 4px; text-decoration: none; font-size: 13px; }
"""


@dataclass
class UpdateState:
    """업데이트 확인/설치의 진행 상태와 그 조작 로직을 함께 갖는다.

    main.py는 이 클래스의 메서드만 부르면 되고, "GitHub에서 어떻게 확인하고
    적용하는지"는 updater.py에, "지금 확인 중인지/뭘 찾았는지"는 여기 상태로
    남는다 — main.py에 클로저로 흩어져 있던 걸 상태 소유자 쪽으로 옮겼다.
    """

    pending: UpdateInfo | None = None
    checking: bool = False

    def check(self, app_dir: Path, channel: str) -> UpdateInfo | None:
        """channel 기준으로 새 버전을 확인하고 pending을 갱신한다. 중복 호출은 무시."""
        if self.checking:
            return self.pending
        self.checking = True
        try:
            self.pending = updater.check_for_update(app_dir, channel=channel)
            return self.pending
        finally:
            self.checking = False

    def install(self, app_dir: Path, on_before_exit: Callable[[], None] | None = None) -> None:
        """pending이 있으면 적용한다. 없으면 아무 일도 하지 않는다."""
        if self.pending is None:
            return
        updater.apply_update(self.pending, app_dir, on_before_exit=on_before_exit)


@dataclass
class WebUIContext:
    """웹 UI 핸들러가 필요로 하는 모든 의존성을 한데 묶은 것.

    main.py가 이 값들을 채워서 넘겨준다 — webui.py는 main.py의 내부 구조를
    몰라도 되고, main.py는 웹 서버 구현(HTML/라우팅)을 몰라도 되게 분리하기
    위함이다.
    """

    app_dir: Path
    rethink_config_path: Path
    store: DeviceStore
    orchestrator: Orchestrator
    settings: dict
    save_settings: Callable[[], None]
    on_first_configured: Callable[[], None]
    update_state: UpdateState
    check_update: Callable[[], None]
    install_update: Callable[[], None]
    is_autostart_enabled: Callable[[], bool]
    toggle_autostart: Callable[[], None]
    app_version: str
    log_path: Path


def _tail_log(path: Path, max_lines: int = 150) -> str:
    if not path.exists():
        return "(로그 파일이 아직 없습니다)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except OSError as e:
        return f"(로그를 읽는 중 오류: {e})"


def _setup_form_html(error: str = "", submitted_values: dict | None = None) -> str:
    values = submitted_values or {}
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>{APP_NAME} - 최초 설정</title>
<style>{_STYLE}</style></head>
<body>
  <h1>{APP_NAME} 최초 설정</h1>
  <p class="hint">rethink-cloud를 시작하기 전에 MQTT 브로커 정보가 필요합니다.
  이미 운영 중인 브로커(예: Home Assistant의 Mosquitto)의 주소를 입력해주세요.</p>
  {error_html}
  <form method="POST" action="/setup" id="setup-form">
    <div class="row">
      <label>MQTT 브로커 주소 (필수)</label>
      <input type="text" name="mqtt_host" id="mqtt_host" placeholder="192.168.0.50"
             value="{html.escape(values.get('mqtt_host', ''))}" required>
    </div>
    <div class="row">
      <label>포트</label>
      <input type="text" name="mqtt_port" id="mqtt_port"
             value="{html.escape(values.get('mqtt_port', '1883'))}" style="width:80px">
      <button type="button" class="secondary" id="test-btn">연결 테스트</button>
      <span id="test-result" class="hint"></span>
    </div>
    <div class="row">
      <label>사용자 이름 (선택)</label>
      <input type="text" name="mqtt_user" value="{html.escape(values.get('mqtt_user', ''))}">
      <label>비밀번호 (선택)</label>
      <input type="password" name="mqtt_pass" value="{html.escape(values.get('mqtt_pass', ''))}">
    </div>
    <div class="row">
      <label>rethink 호스트 이름 (선택, 비워두면 자동)</label>
      <input type="text" name="hostname" placeholder="rethink.lan"
             value="{html.escape(values.get('hostname', ''))}">
    </div>
    <div class="row"><button type="submit">설정 완료</button></div>
  </form>
  <script>
    document.getElementById('test-btn').addEventListener('click', async () => {{
      const btn = document.getElementById('test-btn');
      const result = document.getElementById('test-result');
      const host = document.getElementById('mqtt_host').value.trim();
      const port = document.getElementById('mqtt_port').value.trim() || '1883';
      if (!host) {{ result.textContent = '먼저 MQTT 브로커 주소를 입력해주세요.'; return; }}
      btn.disabled = true;
      result.textContent = '연결 확인 중...';
      try {{
        const resp = await fetch('/setup/test', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
          body: new URLSearchParams({{mqtt_host: host, mqtt_port: port}}),
        }});
        const data = await resp.json();
        result.style.color = data.ok ? '#1e7e34' : '#b3261e';
        result.textContent = data.message;
      }} catch (e) {{
        result.style.color = '#b3261e';
        result.textContent = '테스트 요청 실패: ' + e;
      }} finally {{ btn.disabled = false; }}
    }});
  </script>
</body></html>"""


def _device_row_html(device: Device) -> str:
    enabled_label = "예" if device.enabled else "아니오"
    toggle_label = "비활성화" if device.enabled else "활성화"
    rethink_id_html = (
        html.escape(device.rethink_device_id)
        if device.rethink_device_id
        else '<span class="hint">(없음)</span>'
    )
    return f"""<tr>
      <td>{html.escape(device.name)}</td>
      <td>{html.escape(device.mac)}</td>
      <td>{html.escape(device.ip)}</td>
      <td>{rethink_id_html}</td>
      <td>{enabled_label}</td>
      <td>
        <form class="inline" method="POST" action="/devices/toggle">
          <input type="hidden" name="mac" value="{html.escape(device.mac)}">
          <button type="submit" class="secondary">{toggle_label}</button>
        </form>
        <form class="inline" method="POST" action="/devices/set-rethink-id" style="margin-left:4px">
          <input type="hidden" name="mac" value="{html.escape(device.mac)}">
          <input type="text" name="rethink_device_id" placeholder="rethink ID"
                 value="{html.escape(device.rethink_device_id)}" style="width:110px">
          <button type="submit" class="secondary">저장</button>
        </form>
        <form class="inline" method="POST" action="/devices/remove" style="margin-left:4px"
              onsubmit="return confirm('{html.escape(device.name)} 를 삭제할까요? LG 공식 서버로 되돌아갑니다.');">
          <input type="hidden" name="mac" value="{html.escape(device.mac)}">
          <button type="submit" class="danger">삭제</button>
        </form>
      </td>
    </tr>"""


def _dashboard_html(context: WebUIContext, error: str = "") -> str:
    management_port = context.store.rethink_ports["management_port"]
    device_rows_html = "\n".join(
        _device_row_html(device) for device in context.store.devices
    ) or '<tr><td colspan="6" class="hint">등록된 기기가 없습니다.</td></tr>'

    channel = context.settings.get("update_channel", "stable")
    stable_checked = "checked" if channel == "stable" else ""
    beta_checked = "checked" if channel == "beta" else ""

    pending_update = context.update_state.pending
    if context.update_state.checking:
        update_section_html = '<p class="hint">업데이트 확인 중...</p>'
    elif pending_update:
        install_kind = "전체 재설치" if pending_update.requires_full else "간단 업데이트"
        update_section_html = f"""<p>새 버전 <b>{html.escape(pending_update.version)}</b> 이 있습니다 ({install_kind}).</p>
        <form method="POST" action="/update/install">
          <button type="submit">지금 설치 (앱이 재시작됩니다)</button>
        </form>"""
    else:
        update_section_html = '<p class="hint">확인된 새 버전이 없습니다.</p>'

    autostart_checked = "checked" if context.is_autostart_enabled() else ""
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>{APP_NAME}</title>
<style>{_STYLE}</style></head>
<body>
  <h1>{APP_NAME} <span class="badge">v{html.escape(context.app_version)}</span></h1>
  <a class="link-btn" href="http://127.0.0.1:{management_port}/" target="_blank">rethink 관리 화면 열기 ↗</a>
  {error_html}

  <h2>기기 관리</h2>
  <table>
    <tr><th>이름</th><th>MAC</th><th>IP</th><th>rethink ID</th><th>활성화</th><th>동작</th></tr>
    {device_rows_html}
  </table>
  <form method="POST" action="/devices/add" class="row" style="margin-top:12px">
    <input type="text" name="name" placeholder="이름" required style="width:100px">
    <input type="text" name="mac" placeholder="AA:BB:CC:11:22:33" required style="width:150px">
    <input type="text" name="ip" placeholder="192.168.0.101" required style="width:120px">
    <input type="text" name="rethink_device_id" placeholder="rethink ID (선택)" style="width:130px">
    <button type="submit">추가</button>
  </form>
  <p class="hint">rethink ID는 위 "rethink 관리 화면"의 Connected devices 표 ID 컬럼 값입니다.
  채워두면 비활성화/삭제 시 bridge를 자동으로 꺼줍니다.</p>

  <h2>업데이트</h2>
  <form method="POST" action="/update/channel" class="row">
    <label><input type="radio" name="channel" value="stable" {stable_checked}
      onchange="this.form.submit()"> Stable (정식 릴리즈만)</label>
    <label><input type="radio" name="channel" value="beta" {beta_checked}
      onchange="this.form.submit()"> Beta (수동 빌드 포함)</label>
  </form>
  {update_section_html}
  <form method="POST" action="/update/check">
    <button type="submit" class="secondary">지금 확인</button>
  </form>

  <h2>시작 옵션</h2>
  <form method="POST" action="/autostart/toggle">
    <label><input type="checkbox" name="enabled" value="1" {autostart_checked}
      onchange="this.form.submit()"> Windows 시작 시 자동 실행</label>
  </form>

  <h2>로그 (최근 150줄)</h2>
  <pre class="log">{html.escape(_tail_log(context.log_path))}</pre>
  <p><a href="/">새로고침</a></p>
</body></html>"""


def _setup_complete_html() -> str:
    return (
        '<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="2;url=/">'
        f"<title>설정 완료</title><style>{_STYLE}</style></head>"
        "<body><h1>설정 완료!</h1><p>rethink-cloud를 시작합니다...</p></body></html>"
    )


def _update_installing_html() -> str:
    return (
        '<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">'
        f"<title>업데이트 설치 중</title><style>{_STYLE}</style></head>"
        "<body><h1>업데이트를 설치합니다</h1>"
        "<p>앱이 종료됐다가 새 버전으로 자동 재시작됩니다. 잠시만 기다려주세요.</p>"
        "</body></html>"
    )


def _make_handler(context: WebUIContext):
    class Handler(BaseHTTPRequestHandler):
        # 경로 -> 처리 메서드 이름. do_POST가 참조하는 라우팅 테이블 —
        # 새 액션을 추가할 땐 여기 한 줄과 그 이름의 _handle_* 메서드만
        # 추가하면 된다 (if/elif 사슬을 계속 늘리지 않기 위함).
        POST_ROUTES = {
            "/setup": "_handle_setup",
            "/setup/test": "_handle_setup_test",
            "/devices/add": "_handle_device_add",
            "/devices/remove": "_handle_device_remove",
            "/devices/toggle": "_handle_device_toggle",
            "/devices/set-rethink-id": "_handle_device_set_rethink_id",
            "/update/channel": "_handle_update_channel",
            "/update/check": "_handle_update_check",
            "/update/install": "_handle_update_install",
            "/autostart/toggle": "_handle_autostart_toggle",
        }

        def log_message(self, fmt, *args):  # noqa: A003
            logger.info("[webui] " + fmt, *args)

        # -- 응답 헬퍼 ----------------------------------------------------

        def _send_html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect_home(self) -> None:
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def _read_form(self) -> dict:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length).decode("utf-8")
            return {key: values[0] for key, values in parse_qs(raw_body).items()}

        def _is_configured(self) -> bool:
            return rethink_config.is_configured(context.rethink_config_path)

        # -- 라우팅 ---------------------------------------------------------

        def do_GET(self):  # noqa: N802
            if self.path not in ("/", ""):
                self.send_response(404)
                self.end_headers()
                return
            if self._is_configured():
                self._send_html(_dashboard_html(context))
            else:
                self._send_html(_setup_form_html())

        def do_POST(self):  # noqa: N802
            form = self._read_form()
            handler_name = self.POST_ROUTES.get(self.path)
            if handler_name is None:
                self.send_response(404)
                self.end_headers()
                return

            handler = getattr(self, handler_name)
            try:
                handler(form)
            except DeviceValidationError as e:
                self._send_html(_dashboard_html(context, error=str(e)))
            except Exception as e:  # noqa: BLE001
                logger.error("웹 UI 요청 처리 중 오류 (%s): %s", self.path, e)
                self._send_html(_dashboard_html(context, error=f"오류: {e}"))

        # -- 액션 핸들러 -----------------------------------------------------
        # 전부 (self, form) 시그니처로 통일해서 위 라우팅 테이블에서 인자 개수를
        # 신경 쓸 필요가 없게 한다 — form을 안 쓰는 핸들러도 받기만 하고 무시한다.

        def _handle_setup(self, form: dict) -> None:
            errors = rethink_config.validate(form)  # type: ignore[arg-type]
            if errors:
                self._send_html(
                    _setup_form_html(error=" / ".join(errors), submitted_values=form)
                )
                return
            rethink_config.write_config(context.rethink_config_path, form)  # type: ignore[arg-type]
            self._send_html(_setup_complete_html())
            threading.Thread(target=context.on_first_configured, daemon=True).start()

        def _handle_setup_test(self, form: dict) -> None:
            host = form.get("mqtt_host", "").strip()
            port_text = form.get("mqtt_port", "").strip() or "1883"
            if not host:
                self._send_json(400, {"ok": False, "message": "MQTT 브로커 주소를 입력해주세요."})
                return
            if not port_text.isdigit():
                self._send_json(400, {"ok": False, "message": "포트는 숫자여야 합니다."})
                return
            ok, message = rethink_config.test_mqtt_reachable(host, int(port_text))
            self._send_json(200, {"ok": ok, "message": message})

        def _handle_device_add(self, form: dict) -> None:
            context.orchestrator.add_device(
                form.get("name", ""),
                form.get("mac", ""),
                form.get("ip", ""),
                form.get("rethink_device_id", ""),
            )
            self._redirect_home()

        def _handle_device_remove(self, form: dict) -> None:
            context.orchestrator.remove_device(form.get("mac", ""))
            self._redirect_home()

        def _handle_device_toggle(self, form: dict) -> None:
            mac = form.get("mac", "")
            device = context.store.find(mac)
            if device:
                context.orchestrator.set_device_enabled(mac, not device.enabled)
            self._redirect_home()

        def _handle_device_set_rethink_id(self, form: dict) -> None:
            context.store.set_rethink_device_id(
                form.get("mac", ""), form.get("rethink_device_id", "")
            )
            self._redirect_home()

        def _handle_update_channel(self, form: dict) -> None:
            channel = form.get("channel", "stable")
            if channel not in ("stable", "beta"):
                channel = "stable"
            context.settings["update_channel"] = channel
            context.save_settings()
            context.update_state.pending = None
            self._redirect_home()

        def _handle_update_check(self, form: dict) -> None:
            context.check_update()
            self._redirect_home()

        def _handle_update_install(self, form: dict) -> None:
            if context.update_state.pending is None:
                self._redirect_home()
                return
            self._send_html(_update_installing_html())
            threading.Thread(target=context.install_update, daemon=True).start()

        def _handle_autostart_toggle(self, form: dict) -> None:
            context.toggle_autostart()
            self._redirect_home()

    return Handler


class WebUIServer:
    def __init__(self, context: WebUIContext, bind_port: int = 44490):
        self.context = context
        self.bind_port = bind_port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def url(self) -> str:
        return f"http://127.0.0.1:{self.bind_port}/"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        handler = _make_handler(self.context)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.bind_port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="webui-server"
        )
        self._thread.start()
        logger.info("웹 UI 시작: %s", self.url())

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
