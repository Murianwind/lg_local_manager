"""
devices.json 을 편집하는 아주 단순한 Tkinter 창.

파일을 직접 텍스트 에디터로 열어 편집해도 되지만, 실수(MAC 형식 오타 등)를
줄이기 위해 최소한의 폼 UI를 제공한다.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .device_store import DeviceStore, DeviceValidationError
from .orchestrator import Orchestrator


class DeviceWindow:
    def __init__(self, store: DeviceStore, orchestrator: Orchestrator):
        self.store = store
        self.orchestrator = orchestrator
        self._root: tk.Tk | None = None
        self._thread: threading.Thread | None = None

    def is_open(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_in_thread(self) -> None:
        self._thread = threading.Thread(target=self._build_and_run, daemon=True)
        self._thread.start()

    # -- 내부 구현 --------------------------------------------------

    def _build_and_run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("LG Local Manager - 기기 관리")
        root.geometry("560x360")

        columns = ("name", "mac", "ip", "enabled")
        tree = ttk.Treeview(root, columns=columns, show="headings", height=10)
        for col, label, width in (
            ("name", "이름", 140),
            ("mac", "MAC 주소", 160),
            ("ip", "IP 주소", 130),
            ("enabled", "활성화", 70),
        ):
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor="center")
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        def refresh():
            tree.delete(*tree.get_children())
            for d in self.store.devices:
                tree.insert(
                    "",
                    "end",
                    iid=d.mac,
                    values=(d.name, d.mac, d.ip, "예" if d.enabled else "아니오"),
                )

        refresh()

        form = ttk.Frame(root)
        form.pack(fill="x", padx=8, pady=(0, 8))

        name_var = tk.StringVar()
        mac_var = tk.StringVar()
        ip_var = tk.StringVar()

        ttk.Label(form, text="이름").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=name_var, width=16).grid(row=0, column=1, padx=4)
        ttk.Label(form, text="MAC").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=mac_var, width=20).grid(row=0, column=3, padx=4)
        ttk.Label(form, text="IP").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=ip_var, width=16).grid(row=0, column=5, padx=4)

        def do_add():
            try:
                self.orchestrator.add_device(
                    name_var.get(), mac_var.get(), ip_var.get()
                )
                name_var.set("")
                mac_var.set("")
                ip_var.set("")
                refresh()
            except DeviceValidationError as e:
                messagebox.showerror("입력 오류", str(e))

        def do_remove():
            sel = tree.selection()
            if not sel:
                return
            mac = sel[0]
            if not messagebox.askyesno(
                "삭제 확인",
                "이 기기를 삭제하면 로컬 제어가 중단되고 LG 공식 서버로 되돌아갑니다.\n"
                "먼저 rethink 웹 UI에서 이 기기의 bridge를 껐는지 확인하셨나요?",
            ):
                return
            try:
                self.orchestrator.remove_device(mac)
                refresh()
            except DeviceValidationError as e:
                messagebox.showerror("오류", str(e))

        def do_toggle():
            sel = tree.selection()
            if not sel:
                return
            mac = sel[0]
            current = next(d for d in self.store.devices if d.mac == mac)
            self.orchestrator.set_device_enabled(mac, not current.enabled)
            refresh()

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="추가", command=do_add).pack(side="left")
        ttk.Button(btns, text="활성/비활성 전환", command=do_toggle).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="삭제", command=do_remove).pack(side="left")
        ttk.Button(btns, text="새로고침", command=refresh).pack(side="right")

        root.mainloop()
