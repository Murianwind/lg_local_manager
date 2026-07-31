"""
LG Local Manager - 트레이 앱 진입점.

python -m app.main 으로 실행하거나, 빌드된 exe(LGLocalManager.exe)를 실행한다.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from . import prereq, startup
from .device_store import DeviceStore, watch
from .gui import DeviceWindow
from .orchestrator import Orchestrator

APP_NAME = "LG Local Manager"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def app_dir() -> Path:
    """빌드된 exe 기준 폴더 (개발 중엔 소스 폴더)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = app_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging() -> None:
    log_path = data_dir() / "lglocalmanager.log"
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def make_tray_icon() -> Image.Image:
    """간단한 원형 아이콘을 코드로 생성한다 (별도 이미지 파일 불필요)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(30, 136, 229, 255))
    d.text((20, 22), "LG", fill=(255, 255, 255, 255))
    return img


def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")

    if not prereq.is_admin():
        logger.warning("관리자 권한이 아닙니다. 재실행합니다.")
        prereq.relaunch_as_admin()
        return

    problems = prereq.check_all(app_dir())
    for p in problems:
        logger.error("사전 점검 실패: %s", p)
    # Npcap 미설치 등은 치명적이지 않을 수 있으니(설정 화면 접근은 가능하게)
    # 여기서 종료하지 않고, GUI/트레이에서 경고만 표시한다.

    store_path = data_dir() / "devices.json"
    store = DeviceStore.load(store_path)

    # config/settings.json 에서 게이트웨이 IP, rethink 호스트 등을 읽어온다.
    # 최초 실행 시엔 config.example.json을 참고해 사용자가 채워야 한다.
    import json

    settings_path = app_dir() / "config" / "settings.json"
    settings = {
        "gateway_ip": "192.168.0.1",
        "rethink_host": "127.0.0.1",
    }
    if settings_path.exists():
        settings.update(json.loads(settings_path.read_text(encoding="utf-8")))
    else:
        logger.warning(
            "config/settings.json 이 없어 기본값을 사용합니다: %s", settings
        )

    orchestrator = Orchestrator(
        store=store,
        gateway_ip=settings["gateway_ip"],
        rethink_host=settings["rethink_host"],
        runtime_dir=app_dir() / "runtime",
        config_dir=app_dir() / "config",
        notify=lambda title, msg: icon.notify(msg, title) if icon else None,
    )

    watch(store, orchestrator.on_devices_changed)
    orchestrator.start()

    gui_lock = threading.Lock()
    gui_window: DeviceWindow | None = None

    def open_devices_window(_icon=None, _item=None):
        nonlocal gui_window
        with gui_lock:
            if gui_window is None or not gui_window.is_open():
                gui_window = DeviceWindow(store, orchestrator)
                gui_window.run_in_thread()

    def open_rethink_ui(_icon=None, _item=None):
        import webbrowser

        port = store.rethink_ports["mgmt_port"]
        webbrowser.open(f"http://127.0.0.1:{port}")

    def open_log(_icon=None, _item=None):
        import os

        os.startfile(data_dir() / "lglocalmanager.log")  # noqa: S606 (Windows 전용)

    def quit_app(icon_, _item=None):
        orchestrator.stop()
        icon_.stop()

    def is_autostart_enabled(_item=None) -> bool:
        try:
            return startup.is_registered()
        except Exception:  # noqa: BLE001
            return False

    def toggle_autostart(icon_, _item=None):
        try:
            if startup.is_registered():
                startup.unregister()
                icon_.notify("시작 프로그램 등록을 해제했습니다.", APP_NAME)
            else:
                startup.register()
                icon_.notify("Windows 시작 시 자동으로 실행되도록 등록했습니다.", APP_NAME)
        except Exception as e:  # noqa: BLE001
            logger.error("시작 프로그램 등록/해제 실패: %s", e)
            icon_.notify(f"시작 프로그램 설정 실패: {e}", APP_NAME)

    menu = pystray.Menu(
        pystray.MenuItem("기기 관리...", open_devices_window, default=True),
        pystray.MenuItem("rethink 웹 UI 열기", open_rethink_ui),
        pystray.MenuItem("로그 열기", open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Windows 시작 시 자동 실행",
            toggle_autostart,
            checked=is_autostart_enabled,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", quit_app),
    )

    icon = pystray.Icon(APP_NAME, make_tray_icon(), APP_NAME, menu)

    if problems:
        icon.notify("사전 점검에서 문제가 발견되었습니다. 로그를 확인하세요.", APP_NAME)

    icon.run()


if __name__ == "__main__":
    main()
