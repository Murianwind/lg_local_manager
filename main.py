"""
LG Local Manager - 트레이 앱 진입점.

python -m app.main 으로 실행하거나, 빌드된 exe(LGLocalManager.exe)를 실행한다.

기기 관리/업데이트/자동 시작 같은 조작은 전부 webui.py가 띄우는 로컬 웹
페이지(기본 http://127.0.0.1:44490/)에서 이루어진다. 트레이 메뉴는 그 페이지를
여는 것과 종료, 두 가지만 남긴다. 이 파일이 하는 일은 그 조각들(설정, 저장소,
오케스트레이터, 웹 서버, 트레이 아이콘)을 조립하는 것뿐이다 — 각 조각의 세부
로직은 해당 모듈(settings.py, device_store.py, orchestrator.py, webui.py)에 있다.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import prereq, rethink_config, startup, updater
from .device_store import DeviceStore, watch
from .orchestrator import Orchestrator
from .settings import AppSettings
from .version import APP_NAME, APP_VERSION
from .webui import UpdateState, WebUIContext, WebUIServer

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def app_dir() -> Path:
    """빌드된 exe 기준 폴더 (개발 중엔 소스 폴더)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def data_dir(base_dir: Path) -> Path:
    data_path = base_dir / "data"
    data_path.mkdir(parents=True, exist_ok=True)
    return data_path


def make_tray_icon() -> Image.Image:
    """간단한 원형 아이콘을 코드로 생성한다 (별도 이미지 파일 불필요)."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(30, 136, 229, 255))
    draw.text((20, 22), "LG", fill=(255, 255, 255, 255))
    return image


def setup_logging(log_path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def open_browser_after_delay(url: str, delay_sec: float = 1.0) -> None:
    """서버가 뜰 시간을 잠깐 준 뒤 브라우저를 연다. 백그라운드 스레드에서 호출한다."""
    time.sleep(delay_sec)
    webbrowser.open(url)


def main() -> None:
    base_dir = app_dir()
    data_path = data_dir(base_dir)
    log_path = data_path / "lglocalmanager.log"

    setup_logging(log_path)
    logger = logging.getLogger("main")

    if not prereq.is_admin():
        logger.warning("관리자 권한이 아닙니다. 재실행합니다.")
        prereq.relaunch_as_admin()
        return

    problems = prereq.check_all(base_dir)
    for problem in problems:
        logger.error("사전 점검 실패: %s", problem)
    # Npcap 미설치 등은 치명적이지 않을 수 있으니(설정 화면 접근은 가능하게)
    # 여기서 종료하지 않고, 대시보드/트레이에서 경고만 표시한다.

    store = DeviceStore.load(data_path / "devices.json")
    settings = AppSettings.load(base_dir / "config" / "settings.json")

    orchestrator = Orchestrator(
        store=store,
        gateway_ip=settings.get("gateway_ip"),
        rethink_host=settings.get("rethink_host"),
        runtime_dir=base_dir / "runtime",
        config_dir=base_dir / "config",
        notify=lambda title, message: icon.notify(message, title) if icon else None,
    )
    watch(store, orchestrator.on_devices_changed)

    rethink_config_path = base_dir / "config" / "rethink-config.json"
    update_state = UpdateState()

    def check_update() -> None:
        channel = settings.values.get("update_channel", "stable")
        info = update_state.check(base_dir, channel)
        if info:
            kind = "전체 재설치" if info.requires_full else "간단 업데이트"
            logger.info("업데이트 확인 결과: 새 버전 %s 있음 (%s)", info.version, kind)
            if icon:
                icon.notify(f"새 버전 {info.version} 이 있습니다 ({kind}).", APP_NAME)
        else:
            logger.info("업데이트 확인 결과: 이미 최신 버전입니다 (현재 v%s)", APP_VERSION)

    def install_update() -> None:
        try:
            update_state.install(base_dir, on_before_exit=lambda: quit_app(icon))
        except Exception as e:  # noqa: BLE001
            logger.error("업데이트 적용 실패: %s", e)

    def is_autostart_enabled() -> bool:
        try:
            return startup.is_registered()
        except Exception:  # noqa: BLE001
            return False

    def toggle_autostart() -> None:
        try:
            if startup.is_registered():
                startup.unregister()
            else:
                startup.register()
        except Exception as e:  # noqa: BLE001
            logger.error("시작 프로그램 등록/해제 실패: %s", e)

    def start_rethink_and_devices() -> None:
        orchestrator.start()
        if icon:
            icon.notify("설정이 완료되어 rethink-cloud를 시작합니다.", APP_NAME)

    webui_context = WebUIContext(
        app_dir=base_dir,
        rethink_config_path=rethink_config_path,
        store=store,
        orchestrator=orchestrator,
        settings=settings.values,
        save_settings=settings.save,
        on_first_configured=start_rethink_and_devices,
        update_state=update_state,
        check_update=check_update,
        install_update=install_update,
        is_autostart_enabled=is_autostart_enabled,
        toggle_autostart=toggle_autostart,
        app_version=APP_VERSION,
        log_path=log_path,
    )
    webui_server = WebUIServer(webui_context)
    webui_server.start()

    if rethink_config.is_configured(rethink_config_path):
        orchestrator.start()
    else:
        logger.info(
            "rethink-config.json이 없거나 MQTT 브로커 주소가 비어 있어, "
            "웹 UI에서 최초 설정이 필요합니다: %s",
            webui_server.url(),
        )
        threading.Thread(
            target=open_browser_after_delay, args=(webui_server.url(),), daemon=True
        ).start()

    def open_dashboard(_icon=None, _menu_item=None):
        webbrowser.open(webui_server.url())

    def quit_app(icon_, _menu_item=None):
        webui_server.stop()
        orchestrator.stop()
        icon_.stop()

    def restart_app(icon_, _menu_item=None):
        logger.info("사용자 요청으로 재시작합니다.")
        # sys.argv[0]은 frozen exe에서도, 개발 모드(python -m app.main)에서도
        # 실행 파일 자체를 가리키므로 빼고, 나머지 인자만 sys.executable에 붙여
        # 같은 방식으로 재실행한다.
        subprocess.Popen(
            [sys.executable, *sys.argv[1:]],
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        quit_app(icon_)

    menu = pystray.Menu(
        pystray.MenuItem("대시보드 열기", open_dashboard, default=True),
        pystray.MenuItem("재시작", restart_app),
        pystray.MenuItem("종료", quit_app),
    )

    # NOTE: 아래 클로저(orchestrator의 notify, check_update, install_update)는
    # icon이 여기서 만들어지기 전부터 icon을 참조한다. 파이썬 클로저는 이름을
    # "호출되는 시점"에 찾기 때문에 문제 없다 — 실제로 그 클로저들이 불리는
    # 시점(웹 UI에서 사용자가 뭔가를 누른 뒤)에는 icon이 이미 대입되어 있다.
    icon = pystray.Icon(APP_NAME, make_tray_icon(), APP_NAME, menu)

    if problems:
        icon.notify(
            "사전 점검에서 문제가 발견되었습니다. 대시보드 하단 로그를 확인하세요.",
            APP_NAME,
        )

    icon.run()


if __name__ == "__main__":
    main()
