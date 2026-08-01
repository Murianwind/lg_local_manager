"""
rethink-cloud(Node.js)를 서브프로세스로 기동/감시한다.

rethink 자체는 재구현하지 않는다 (TLV 파서, 인증서 발급, bridge 로직 등이
이미 충분히 성숙해 있고, 원본 프로젝트의 업데이트를 그대로 받아 쓰는 게
유지보수 측면에서 훨씬 안전하다). 이 모듈은 그걸 감싸는 얇은 래퍼일 뿐이다.

배포 시에는 아래 두 가지가 앱 옆에 함께 있어야 한다:
  runtime/node/            <- 포터블 Node.js (설치 없이 압축 해제만으로 실행)
  runtime/rethink/          <- 빌드된 rethink-cloud (npm run build 결과물)
둘 다 GitHub Actions 빌드 단계에서 자동으로 받아서 채워 넣는다
(.github/workflows/build.yml 참고).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from .. import prereq

logger = logging.getLogger("rethink_process")


class RethinkProcess:
    def __init__(
        self,
        node_exe: Path,
        rethink_entry: Path,
        config_path: Path,
        app_dir: Path | None = None,
        cwd: Path | None = None,
        restart_delay_sec: float = 3.0,
        log_callback=None,
    ):
        self.node_exe = node_exe
        self.rethink_entry = rethink_entry
        self.config_path = config_path
        self.app_dir = app_dir
        self.cwd = cwd or rethink_entry.parent
        self.restart_delay_sec = restart_delay_sec
        self.log_callback = log_callback or (lambda line: None)

        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._run_and_watch, daemon=True, name="rethink-process"
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._terminate_current()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _terminate_current(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self._proc.kill()

    def _build_env(self) -> dict:
        env = os.environ.copy()
        openssl_dir = prereq.find_openssl_dir(self.app_dir)
        if openssl_dir:
            env["PATH"] = str(openssl_dir) + os.pathsep + env.get("PATH", "")
            logger.info("openssl 경로를 PATH에 추가: %s", openssl_dir)
            cnf = prereq.openssl_cnf_path(openssl_dir)
            if cnf:
                env["OPENSSL_CONF"] = str(cnf)
                logger.info("OPENSSL_CONF 설정: %s", cnf)
            else:
                logger.warning(
                    "openssl.cnf를 찾지 못했습니다 (%s/../ssl/openssl.cnf 없음) — "
                    "openssl이 설정 파일을 못 찾아 실패할 수 있습니다.",
                    openssl_dir,
                )
        else:
            logger.warning(
                "openssl.exe를 찾지 못했습니다 — rethink-cloud의 인증서 발급이 "
                "실패할 수 있습니다 (Git for Windows 설치를 권장합니다)."
            )
        return env

    def _run_and_watch(self) -> None:
        while not self._stop_event.is_set():
            try:
                logger.info("rethink-cloud 시작: %s", self.rethink_entry)
                self._proc = subprocess.Popen(
                    [
                        str(self.node_exe),
                        str(self.rethink_entry),
                        str(self.config_path),
                    ],
                    cwd=str(self.cwd),
                    env=self._build_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0,
                )
                if self._proc.stdout is None:
                    raise RuntimeError("subprocess stdout이 파이프로 연결되지 않았습니다.")
                for line in self._proc.stdout:
                    self.log_callback(line.rstrip("\n"))
                    if self._stop_event.is_set():
                        break
                self._proc.wait()
                exit_code = self._proc.returncode
                logger.warning("rethink-cloud 프로세스 종료 (exit=%s)", exit_code)
            except FileNotFoundError as e:
                logger.error("rethink-cloud 실행 파일을 찾을 수 없습니다: %s", e)
                self._stop_event.wait(self.restart_delay_sec)
            except Exception as e:  # noqa: BLE001
                logger.error("rethink-cloud 실행 중 오류: %s", e)

            if self._stop_event.is_set():
                break
            # 비정상 종료 시 자동 재시작 — stop()이 이 대기 중에 불려도 즉시 깨어나도록
            # time.sleep 대신 인터럽트 가능한 wait을 쓴다.
            self._stop_event.wait(self.restart_delay_sec)
