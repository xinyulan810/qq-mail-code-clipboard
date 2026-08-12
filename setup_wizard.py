#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
首次设置向导：引导用户开启 QQ 邮箱 IMAP、获取授权码，测试连接后保存配置。
"""

import json
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import main as engine

GUIDE_TEXT = (
    "开启 QQ 邮箱 IMAP 并获取授权码：\n"
    "1. 浏览器登录 https://mail.qq.com\n"
    "2. 设置 → 账号 → 开启 IMAP/SMTP 服务\n"
    "3. 按提示完成短信验证，获得 16 位授权码\n"
    "4. 把邮箱和授权码填到下面，点“测试连接”验证"
)


class Wizard:
    def __init__(self, root: tk.Tk, config_path: Path, error: str = ""):
        self.root = root
        self.config_path = Path(config_path)
        self.saved = False

        root.title("QQ 验证码工具 - 设置")
        root.resizable(False, False)
        root.configure(bg="#FFFFFF")
        self._build(error)
        self._center()

    def _build(self, error: str) -> None:
        outer = tk.Frame(self.root, bg="#FFFFFF", padx=26, pady=22)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text="QQ 邮箱验证码工具",
            bg="#FFFFFF",
            fg="#0B57D0",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            outer,
            text="首次使用需要配置 QQ 邮箱，只需设置一次",
            bg="#FFFFFF",
            fg="#6B7280",
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(2, 14))

        guide = tk.Frame(outer, bg="#EAF2FF", padx=14, pady=10)
        guide.pack(fill="x")
        tk.Label(
            guide,
            text=GUIDE_TEXT,
            bg="#EAF2FF",
            fg="#334155",
            font=("Microsoft YaHei UI", 9),
            justify="left",
        ).pack(anchor="w")

        form = tk.Frame(outer, bg="#FFFFFF")
        form.pack(fill="x", pady=(16, 0))

        # 邮箱
        tk.Label(form, text="QQ 邮箱地址", bg="#FFFFFF", fg="#374151",
                 font=("Microsoft YaHei UI", 10)).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.email_var = tk.StringVar()
        email_entry = ttk.Entry(form, textvariable=self.email_var, width=38, font=("Microsoft YaHei UI", 10))
        email_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8), padx=(12, 0))

        # 授权码
        tk.Label(form, text="IMAP 授权码", bg="#FFFFFF", fg="#374151",
                 font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, sticky="w", pady=(0, 8))
        self.auth_var = tk.StringVar()
        auth_frame = tk.Frame(form, bg="#FFFFFF")
        auth_frame.grid(row=1, column=1, sticky="ew", pady=(0, 8), padx=(12, 0))
        self.auth_entry = ttk.Entry(auth_frame, textvariable=self.auth_var, width=32, show="*",
                                    font=("Consolas", 10))
        self.auth_entry.pack(side="left", fill="x", expand=True)
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(auth_frame, text="显示", variable=self.show_var,
                        command=self._toggle_show).pack(side="left", padx=(8, 0))

        # 服务器（高级）
        tk.Label(form, text="服务器 / 端口", bg="#FFFFFF", fg="#374151",
                 font=("Microsoft YaHei UI", 10)).grid(row=2, column=0, sticky="w")
        server_frame = tk.Frame(form, bg="#FFFFFF")
        server_frame.grid(row=2, column=1, sticky="w", padx=(12, 0))
        self.host_var = tk.StringVar(value="imap.qq.com")
        self.port_var = tk.StringVar(value="993")
        ttk.Entry(server_frame, textvariable=self.host_var, width=24,
                  font=("Consolas", 10)).pack(side="left")
        ttk.Entry(server_frame, textvariable=self.port_var, width=7,
                  font=("Consolas", 10)).pack(side="left", padx=(8, 0))

        form.columnconfigure(1, weight=1)

        # 状态
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(
            outer, textvariable=self.status_var, bg="#FFFFFF", fg="#16A34A",
            font=("Microsoft YaHei UI", 9), justify="left", wraplength=440,
        )
        self.status_label.pack(
            anchor="w", pady=(12, 0)
        )

        # 按钮
        btns = tk.Frame(outer, bg="#FFFFFF")
        btns.pack(fill="x", pady=(14, 0))
        self.test_btn = ttk.Button(btns, text="测试连接", command=self._test)
        self.test_btn.pack(side="left")
        ttk.Button(btns, text="保存并启动", command=self._save).pack(side="right")

        tk.Label(
            outer,
            text="授权码仅保存在本机配置文件，不会上传到任何地方",
            bg="#FFFFFF",
            fg="#9CA3AF",
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", pady=(10, 0))

        if error:
            self._status(error, "#E5484D")

        self._prefill()

    def _prefill(self) -> None:
        if not self.config_path.exists():
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("email"):
            self.email_var.set(str(data["email"]))
        if data.get("auth_code"):
            self.auth_var.set(str(data["auth_code"]))
        if data.get("host"):
            self.host_var.set(str(data["host"]))
        if data.get("port"):
            self.port_var.set(str(data["port"]))

    def _center(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    def _toggle_show(self) -> None:
        self.auth_entry.config(show="" if self.show_var.get() else "*")

    def _status(self, text: str, color: str = "#16A34A") -> None:
        self.status_var.set(text)
        self.status_label.config(fg=color)

    def _validate(self):
        email = self.email_var.get().strip()
        auth = self.auth_var.get().strip()
        if not email or "@" not in email:
            self._status("请填写正确的 QQ 邮箱地址", "#E5484D")
            return None
        if not auth:
            self._status("请填写 IMAP 授权码", "#E5484D")
            return None
        if len(auth) != 16:
            self._status("提示：授权码一般是 16 位，请确认没有填错", "#F29900")
        host = self.host_var.get().strip() or "imap.qq.com"
        try:
            port = int(self.port_var.get().strip() or 993)
        except ValueError:
            port = 993
        return email, auth, host, port

    def _test(self) -> None:
        values = self._validate()
        if values is None:
            return
        email, auth, host, port = values
        self.test_btn.config(state="disabled")
        self._status("正在测试连接…", "#6B7280")
        result_q = queue.Queue()

        def worker():
            try:
                mail = engine.QQIMAP4SSL(host, port, timeout=10)
                mail.login(email, auth)
                mail.select("INBOX")
                typ, data = mail.status("INBOX", "(MESSAGES)")
                text = data[0].decode(errors="replace")
                match = re.search(r"MESSAGES (\d+)", text)
                count = match.group(1) if match else "?"
                caps = [
                    c.decode(errors="replace") if isinstance(c, bytes) else c
                    for c in getattr(mail, "capabilities", ())
                ]
                idle = "IDLE" in caps
                mail.logout()
                result_q.put(
                    ("ok", f"连接成功！收件箱 {count} 封邮件，IDLE 推送：{'支持' if idle else '不支持（将自动轮询）'}")
                )
            except Exception as exc:
                result_q.put(("err", f"连接失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_test(result_q)

    def _poll_test(self, result_q) -> None:
        try:
            kind, msg = result_q.get_nowait()
        except queue.Empty:
            self.root.after(200, lambda: self._poll_test(result_q))
            return
        self.test_btn.config(state="normal")
        self._status(msg, "#16A34A" if kind == "ok" else "#E5484D")

    def _save(self) -> None:
        values = self._validate()
        if values is None:
            return
        email, auth, host, port = values
        cfg = {
            "email": email,
            "auth_code": auth,
            "host": host,
            "port": port,
            "poll_interval": 30,
            "idle_duration": 1500,
            "mark_seen": False,
            "state_file": ".state.json",
            "extra_patterns": [],
            "platform_keywords": {},
        }
        try:
            self.config_path.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self.saved = True
        self.root.destroy()


def run(config_path: Path, error: str = "") -> bool:
    """打开设置向导；返回 True 表示已保存配置，False 表示用户取消/关闭。"""
    root = tk.Tk()
    wizard = Wizard(root, config_path, error)
    root.mainloop()
    return wizard.saved
