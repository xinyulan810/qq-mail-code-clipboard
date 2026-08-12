#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 邮箱验证码弹窗工具（GUI）

后台用 IMAP IDLE 实时监听邮箱；收到含验证码的新邮件时弹出窗口，
显示发信平台 + 验证码，并提供一键复制按钮。

用法:
  python gui.py            # 启动窗口
  python gui.py --catchup  # 启动时也处理存量未处理邮件
  pythonw gui.py           # 无控制台窗口后台运行
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import main as engine

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
    from PIL import ImageTk

    TRAY_AVAILABLE = True
    CARD_OK = True
except ImportError:
    pystray = None
    TRAY_AVAILABLE = False
    CARD_OK = False

log = logging.getLogger("qq-code-gui")

SINGLE_INSTANCE_PORT = 47301
_MUTEX_HANDLE = None


def _make_tray_icon_image(size: int = 64):
    """生成蓝底白色“码”字图标（托盘/状态窗口共用）。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (2, 2, size - 2, size - 2),
        radius=max(8, size // 5),
        fill="#0b57d0",
    )

    font = None
    for path in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arialbd.ttf",
    ):
        try:
            font = ImageFont.truetype(path, int(size * 0.6))
            break
        except Exception:
            continue

    if font is not None:
        text = "码"
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        draw.text(
            ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]),
            text,
            font=font,
            fill="white",
        )
    else:
        # 兜底画一个钥匙形状
        s = size
        draw.ellipse((s * 0.28, s * 0.16, s * 0.53, s * 0.41), fill="white")
        draw.rectangle((s * 0.41, s * 0.34, s * 0.72, s * 0.56), fill="white")
    return img


# ---------------------------------------------------------------------------
# 弹窗卡片样式
# ---------------------------------------------------------------------------

CARD_W, CARD_H = 440, 316
CARD_MAGIC = "#FF00FF"

_PLATFORM_COLORS = [
    "#0B57D0", "#C5221F", "#0F9D58", "#F29900",
    "#7C3AED", "#0D9488", "#DB2777", "#EA580C",
]


def _platform_color(name: str) -> str:
    """按平台名取一个稳定的主题色。"""
    return _PLATFORM_COLORS[sum(ord(ch) for ch in name) % len(_PLATFORM_COLORS)]


def _make_card_image():
    """生成圆角白色卡片背景。"""
    img = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, CARD_W - 1, CARD_H - 1),
        radius=26,
        fill="#FFFFFF",
        outline="#E4E9F2",
        width=2,
    )
    return img


# ---------------------------------------------------------------------------
# 单实例：已运行时唤起已有窗口，不再启动第二个
# ---------------------------------------------------------------------------

def _acquire_single_instance() -> bool:
    """尝试成为唯一实例。返回 True 表示当前是第一个实例，False 表示已有实例在运行。"""
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is not None:
        return True  # 本进程已持有互斥体（重新配置后再次进入监听）

    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "QQMailCodeWatcher_IMAP_1")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        # 持有句柄直到进程退出，互斥体才会被释放
        _MUTEX_HANDLE = handle
        return True

    # 非 Windows：用 lockfile 兜底
    lock_path = engine.BASE_DIR / ".instance.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(lock_path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
            return False
        except (ValueError, ProcessLookupError):
            try:
                lock_path.unlink()
            except OSError:
                pass
            return _acquire_single_instance()


def _find_window_by_title(title: str):
    """按标题查找顶层窗口（Windows）。"""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    result = []
    wnd_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    @wnd_enum_proc
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == title:
                result.append(hwnd)
                return False  # 停止枚举
        return True

    user32.EnumWindows(callback, 0)
    return result[0] if result else None


def _activate_window(hwnd) -> None:
    """把窗口恢复并带到前台。"""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)   # 模拟按下 Alt，解除前台限制
    user32.keybd_event(0x12, 0, 2, 0)   # 松开 Alt
    user32.SetForegroundWindow(hwnd)
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # 临时置顶
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)  # 取消置顶


def _signal_existing_instance() -> None:
    """告诉已运行的实例把窗口打开到前台。"""
    # 首选本地端口通知：窗口即使收在托盘里也能被唤醒
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2) as conn:
            conn.sendall(b"show\n")
            return
    except OSError:
        pass
    # 兜底：按窗口标题直接恢复
    hwnd = _find_window_by_title("QQ 验证码监听")
    if hwnd is not None:
        _activate_window(hwnd)


# ---------------------------------------------------------------------------
# 发信平台识别
# ---------------------------------------------------------------------------

PLATFORM_KEYWORDS = [
    ("企业微信", ["企业微信", "wecom"]),
    ("微信", ["微信", "weixin", "wechat"]),
    ("QQ", ["【qq】", "qq.com", "qq邮箱", "qq安全中心", "qq登录", "腾讯qq"]),
    ("腾讯", ["腾讯", "tencent"]),
    ("支付宝", ["支付宝", "alipay"]),
    ("淘宝", ["淘宝", "天猫", "taobao", "tmall"]),
    ("拼多多", ["拼多多", "pinduoduo"]),
    ("京东", ["京东", "jd.com", "jingdong"]),
    ("美团", ["美团", "meituan"]),
    ("饿了么", ["饿了么", "ele.me", "eleme"]),
    ("滴滴", ["滴滴", "didi"]),
    ("抖音", ["抖音", "douyin"]),
    ("快手", ["快手", "kuaishou"]),
    ("微博", ["微博", "weibo"]),
    ("知乎", ["知乎", "zhihu"]),
    ("哔哩哔哩", ["哔哩哔哩", "bilibili", "b站"]),
    ("小红书", ["小红书", "xiaohongshu", "redbook"]),
    ("钉钉", ["钉钉", "dingtalk"]),
    ("网易", ["网易", "163.com", "netease"]),
    ("百度", ["百度", "baidu"]),
    ("阿里云", ["阿里云", "aliyun"]),
    ("腾讯云", ["腾讯云", "cloud.tencent", "tencentcloud"]),
    ("华为云", ["华为云", "huaweicloud"]),
    ("AWS", ["amazonaws", "amazon web services"]),
    ("小米", ["小米", "xiaomi", "mi.com"]),
    ("华为", ["华为", "huawei"]),
    ("荣耀", ["荣耀", "honor"]),
    ("OPPO", ["oppo"]),
    ("vivo", ["vivo"]),
    ("三星", ["三星", "samsung"]),
    ("Apple", ["apple", "icloud"]),
    ("微软", ["微软", "microsoft", "outlook", "hotmail"]),
    ("Google", ["google", "gmail"]),
    ("GitHub", ["github"]),
    ("Steam", ["steam"]),
    ("Epic", ["epicgames"]),
    ("Discord", ["discord"]),
    ("Telegram", ["telegram"]),
    ("GLaDOS", ["glados", "glados.network"]),
    ("osu!", ["osu!", "ppy.sh", "osu@ppy.sh"]),
    ("招商银行", ["招商银行", "cmbchina", "cmb"]),
    ("工商银行", ["工商银行", "icbc"]),
    ("建设银行", ["建设银行", "ccb"]),
    ("农业银行", ["农业银行", "abcchina", "95599"]),
    ("中国银行", ["中国银行", "bankofchina", "boc"]),
    ("交通银行", ["交通银行", "bankcomm", "95559"]),
    ("邮储银行", ["邮储银行", "psbc", "95580"]),
    ("兴业银行", ["兴业银行", "cib"]),
    ("浦发银行", ["浦发银行", "spdb", "95528"]),
    ("民生银行", ["民生银行", "cmbc", "95568"]),
    ("中信银行", ["中信银行", "citic", "95558"]),
    ("光大银行", ["光大银行", "cebbank", "95595"]),
    ("平安银行", ["平安银行", "pingan", "95511"]),
    ("广发银行", ["广发银行", "cgbchina", "95508"]),
]


def platform_keywords(cfg=None):
    """内置平台表 + 配置文件覆盖/追加。"""
    merged = {}
    order = []
    for name, keywords in PLATFORM_KEYWORDS:
        if name not in merged:
            order.append(name)
        merged[name] = list(keywords)
    if cfg:
        for name, keywords in (cfg.get("platform_keywords") or {}).items():
            if name not in merged:
                order.append(name)
            merged[name] = [str(k) for k in keywords]
    return [(name, merged[name]) for name in order]


def detect_platform(subject: str, from_header: str, cfg=None) -> str:
    text = f"{subject}\n{from_header}".lower()
    for name, keywords in platform_keywords(cfg):
        for keyword in keywords:
            if keyword.lower() in text:
                return name
    # 识别不到时，直接用发信人名称（一般发信人就是平台名）
    return _sender_name(from_header)


def _sender_name(from_header: str) -> str:
    """从 From 头提取发信人名称：显示名 > 邮箱前缀 > 域名。"""
    if not from_header:
        return "未知平台"
    m = re.search(r'"([^"]+)"\s*<', from_header)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"([^<>\s]+)\s*<", from_header)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"([\w.+-]+)@([\w.-]+)", from_header)
    if m:
        local = m.group(1).split("+")[0].lower()
        if local not in _GENERIC_SENDER_LOCAL:
            return m.group(1)
        return _domain_name(m.group(2))
    return "未知平台"


_GENERIC_SENDER_LOCAL = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notification", "notifications", "notify", "service", "services",
    "support", "account", "accounts", "admin", "info", "contact",
    "mailer", "system", "postmaster", "webmaster", "hello", "welcome",
    "team", "official", "security", "verify", "verification", "nobody",
}

_COMMON_TLDS = {
    "com", "net", "org", "cn", "co", "jp", "uk", "de", "fr", "ru",
    "io", "me", "tv", "xyz", "top", "cc", "info", "biz", "edu", "gov",
}


def _domain_name(domain: str) -> str:
    """从域名提取平台名：no-reply.example.com -> example。"""
    parts = domain.lower().split(".")
    while parts and parts[-1] in _COMMON_TLDS:
        parts.pop()
    if not parts:
        return domain.lower()
    return parts[-1]


# ---------------------------------------------------------------------------
# 验证码弹窗
# ---------------------------------------------------------------------------

class CodePopup(tk.Toplevel):
    """验证码到达时弹出的卡片窗口：平台 + 验证码，点击验证码即可复制。"""

    def __init__(self, app: "App", info: dict):
        super().__init__(app.root)
        self.app = app
        self.info = info
        self._copied = False
        self._drag_offset = None

        self.title(f"{info['platform']} 验证码")
        self.attributes("-topmost", True)
        try:
            self.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        if CARD_OK:
            self._build_card()
        else:
            self._build_plain()

        self.bind("<Return>", lambda _e: self.copy())
        self.bind("<space>", lambda _e: self.copy())
        self.bind("<Escape>", lambda _e: self.close())

        self._center()
        self.lift()
        self.focus_force()

    def _build_card(self) -> None:
        """圆角卡片样式（无边框、可拖动、透明圆角）。"""
        self._bg_photo = ImageTk.PhotoImage(_make_card_image())
        self.configure(bg=CARD_MAGIC)
        try:
            self.attributes("-transparentcolor", CARD_MAGIC)
        except tk.TclError:
            pass
        self.overrideredirect(True)

        canvas = tk.Canvas(
            self,
            width=CARD_W,
            height=CARD_H,
            bg=CARD_MAGIC,
            highlightthickness=0,
            bd=0,
        )
        canvas.pack()
        self._canvas = canvas
        canvas.create_image(0, 0, image=self._bg_photo, anchor="nw")

        color = _platform_color(self.info["platform"])

        # 平台：彩色圆点 + 名称
        canvas.create_oval(30, 34, 44, 48, fill=color, outline="")
        canvas.create_text(
            54,
            41,
            text=self.info["platform"],
            anchor="w",
            fill=color,
            font=("Microsoft YaHei UI", 13, "bold"),
        )

        # 关闭按钮
        close_id = canvas.create_text(
            CARD_W - 28,
            36,
            text="✕",
            fill="#9AA4B2",
            font=("Segoe UI Symbol", 14, "bold"),
        )
        canvas.tag_bind(close_id, "<Button-1>", lambda _e: self.close())
        canvas.tag_bind(
            close_id, "<Enter>", lambda _e: canvas.itemconfig(close_id, fill="#E5484D")
        )
        canvas.tag_bind(
            close_id, "<Leave>", lambda _e: canvas.itemconfig(close_id, fill="#9AA4B2")
        )

        # 验证码：点击复制
        self.code_label = tk.Label(
            canvas,
            text=self.info["code"],
            bg="#FFFFFF",
            fg="#111827",
            font=("Consolas", 32, "bold"),
            cursor="hand2",
        )
        self.code_label.bind("<Button-1>", lambda _e: self.copy())
        self.code_label.bind("<Return>", lambda _e: self.copy())
        self.code_label.bind("<space>", lambda _e: self.copy())
        self.code_label.bind(
            "<Enter>", lambda _e: self.code_label.config(fg=color)
        )
        self.code_label.bind(
            "<Leave>", lambda _e: self.code_label.config(fg="#111827")
        )
        canvas.create_window(CARD_W // 2, 120, window=self.code_label)

        # 提示
        self.hint_label = tk.Label(
            canvas,
            text="点击验证码复制 · Enter 复制 · Esc 关闭",
            bg="#FFFFFF",
            fg="#9AA4B2",
            font=("Microsoft YaHei UI", 9),
        )
        canvas.create_window(CARD_W // 2, 158, window=self.hint_label)

        # 主题
        subject = self.info.get("subject", "")
        if subject:
            subject_label = tk.Label(
                canvas,
                text=subject[:50],
                bg="#FFFFFF",
                fg="#4B5563",
                font=("Microsoft YaHei UI", 10),
                wraplength=CARD_W - 72,
                justify="center",
            )
            canvas.create_window(CARD_W // 2, 202, window=subject_label)

        # 发件人
        sender = self.info.get("from_header", "")
        if sender:
            sender_label = tk.Label(
                canvas,
                text=sender[:60],
                bg="#FFFFFF",
                fg="#8A93A3",
                font=("Microsoft YaHei UI", 9),
                wraplength=CARD_W - 72,
                justify="center",
            )
            canvas.create_window(CARD_W // 2, 240, window=sender_label)

        # 其他候选
        codes = self.info.get("codes", [])
        if len(codes) > 1:
            other_label = tk.Label(
                canvas,
                text="其他候选: " + "、".join(codes[1:]),
                bg="#FFFFFF",
                fg="#B45309",
                font=("Microsoft YaHei UI", 9),
            )
            canvas.create_window(CARD_W // 2, 278, window=other_label)

        # 时间（右下角）
        canvas.create_text(
            CARD_W - 26,
            CARD_H - 16,
            text=time.strftime("%H:%M:%S"),
            anchor="se",
            fill="#C3CAD6",
            font=("Microsoft YaHei UI", 9),
        )

        # 拖动窗口
        canvas.bind("<Button-1>", self._drag_start)
        canvas.bind("<B1-Motion>", self._drag_move)

    def _build_plain(self) -> None:
        """PIL 不可用时的朴素样式。"""
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=self.info["platform"],
            font=("Microsoft YaHei UI", 13, "bold"),
            foreground=_platform_color(self.info["platform"]),
        ).pack()
        self.code_label = tk.Label(
            frame,
            text=self.info["code"],
            font=("Consolas", 28, "bold"),
            fg="#111827",
            cursor="hand2",
        )
        self.code_label.pack(pady=(6, 2))
        self.code_label.bind("<Button-1>", lambda _e: self.copy())

        self.hint_label = tk.Label(
            frame,
            text="点击验证码复制 · Enter 复制 · Esc 关闭",
            fg="#9AA4B2",
        )
        self.hint_label.pack()

        if self.info.get("subject"):
            tk.Label(
                frame,
                text=self.info["subject"][:60],
                wraplength=380,
                fg="#4B5563",
            ).pack(pady=(8, 0))
        if self.info.get("from_header"):
            tk.Label(
                frame,
                text=self.info["from_header"][:70],
                wraplength=380,
                fg="#8A93A3",
            ).pack(pady=(2, 0))
        if len(self.info.get("codes", [])) > 1:
            tk.Label(
                frame,
                text="其他候选: " + "、".join(self.info["codes"][1:]),
                fg="#B45309",
            ).pack(pady=(2, 0))

    def _center(self) -> None:
        if CARD_OK and getattr(self, "_canvas", None) is not None:
            w, h = CARD_W, CARD_H
        else:
            self.update_idletasks()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _drag_start(self, event) -> None:
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_move(self, event) -> None:
        if self._drag_offset is not None:
            x = event.x_root - self._drag_offset[0]
            y = event.y_root - self._drag_offset[1]
            self.geometry(f"+{x}+{y}")

    def copy(self) -> None:
        """点击验证码：复制到剪贴板，成功后自动关闭。"""
        if self._copied:
            return
        try:
            engine.copy_to_clipboard(self.info["code"])
        except Exception as exc:
            self.hint_label.config(text=f"复制失败: {exc}", fg="#E5484D")
            return
        self._copied = True
        self.code_label.config(
            text="已复制 ✓",
            fg="#16A34A",
            font=("Microsoft YaHei UI", 20, "bold"),
        )
        self.hint_label.config(text="即将关闭…", fg="#16A34A")
        self.after(600, self.close)

    def close(self) -> None:
        try:
            self.destroy()
        finally:
            self.app.on_popup_closed()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, cfg: dict, catchup: bool = False):
        self.root = root
        self.cfg = cfg
        self.catchup = catchup
        self.events: queue.Queue = queue.Queue()
        self.pending: list[dict] = []
        self.current: CodePopup | None = None
        self.online = False
        self._start_time = time.time()
        self._tray_icon = None
        self._tray_hidden = False
        self._pump_job = None
        self._tick_job = None
        self.exit_reason = "quit"

        root.title("QQ 验证码监听")
        root.geometry("480x370")
        root.minsize(420, 300)
        root.protocol("WM_DELETE_WINDOW", self._close_to_tray)
        root.bind("<Unmap>", self._on_root_unmap)
        self._build_status()
        self._pump_job = root.after(200, self._pump)
        self._tick_job = root.after(1000, self._tick)
        self._setup_tray()
        self._start_show_listener()
        self._start_watcher()

    def _build_status(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        if CARD_OK:
            self._header_icon = ImageTk.PhotoImage(_make_tray_icon_image(34))
            ttk.Label(header, image=self._header_icon).pack(side="left")
        ttk.Label(
            header,
            text="QQ 邮箱验证码监听",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left", padx=(8, 0))

        status_row = ttk.Frame(outer)
        status_row.pack(anchor="w", pady=(8, 2))
        self.dot_label = tk.Label(status_row, text="●", fg="#F59E0B")
        self.dot_label.pack(side="left")
        self.status_var = tk.StringVar(value=f"启动中 ...（{self.cfg['email']}）")
        ttk.Label(
            status_row,
            textvariable=self.status_var,
            foreground="#0b57d0",
        ).pack(side="left", padx=(6, 0))

        self.uptime_var = tk.StringVar(value="运行时间 00:00:00")
        ttk.Label(
            outer,
            textvariable=self.uptime_var,
            foreground="#888888",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Separator(outer).pack(fill="x", pady=(0, 8))

        ttk.Label(outer, text="最近事件：", foreground="#555555").pack(anchor="w")

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        self.event_list = tk.Listbox(
            list_frame,
            height=8,
            font=("Microsoft YaHei UI", 9),
            activestyle="none",
        )
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.event_list.yview)
        self.event_list.configure(yscrollcommand=scroll.set)
        self.event_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="重新配置", command=self._request_reconfigure).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(buttons, text="退出", command=self.quit).pack(side="right")
        ttk.Button(buttons, text="清空记录", command=self._clear_history).pack(
            side="right", padx=(0, 8)
        )
        ttk.Label(
            outer,
            text="提示：最小化窗口后收进右下角托盘，双击托盘图标恢复",
            foreground="#999999",
        ).pack(anchor="w", pady=(6, 0))

    # ---------------- 系统托盘 ----------------

    def _setup_tray(self) -> None:
        if not TRAY_AVAILABLE:
            log.info("未安装 pystray/Pillow，最小化后停留在任务栏（可执行 pip install pystray pillow）")
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._on_tray_show, default=True),
            pystray.MenuItem("重新配置", self._on_tray_reconfigure),
            pystray.MenuItem("退出", self._on_tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "qq_code_watcher",
            _make_tray_icon_image(),
            "QQ 邮箱验证码监听",
            menu,
        )
        self._tray_icon.run_detached()

    def _on_tray_show(self, _icon, _item) -> None:
        self._post("tray_show")

    def _on_tray_quit(self, _icon, _item) -> None:
        self._post("tray_quit")

    def _on_tray_reconfigure(self, _icon, _item) -> None:
        self._post("reconfigure")

    def _on_root_unmap(self, _event) -> None:
        # 用户点击最小化时，从任务栏收进系统托盘
        if self._tray_icon is not None and not self._tray_hidden:
            # 等窗口管理器完成最小化后再收进托盘，避免状态被覆盖
            self.root.after(120, self._check_hide_to_tray)

    def _check_hide_to_tray(self) -> None:
        if (
            self._tray_icon is not None
            and not self._tray_hidden
            and self.root.state() == "iconic"
        ):
            self._hide_to_tray()

    def _close_to_tray(self) -> None:
        if self._tray_icon is not None:
            self._hide_to_tray()
        else:
            self.quit()

    def _hide_to_tray(self) -> None:
        self._tray_hidden = True
        self.root.withdraw()
        log.info("已最小化到系统托盘，双击托盘图标恢复")
        try:
            self._tray_icon.notify(
                "工具仍在后台运行，双击图标恢复窗口",
                "QQ 验证码监听",
            )
        except Exception:
            pass

    def _restore_window(self) -> None:
        self._tray_hidden = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_stop(self) -> None:
        icon = self._tray_icon
        if icon is not None:
            self._tray_icon = None
            try:
                icon.stop()
            except Exception:
                pass

    def _request_reconfigure(self) -> None:
        """退出当前监听，回到设置向导（重新配置后自动继续监听）。"""
        self.exit_reason = "reconfigure"
        self.quit()

    def _start_show_listener(self) -> None:
        """监听本地端口：第二个实例启动时通知我们打开窗口。"""

        def serve() -> None:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
                server.listen(2)
            except OSError:
                server.close()
                return
            server.settimeout(1)
            try:
                while True:
                    try:
                        conn, _addr = server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    with conn:
                        try:
                            if conn.recv(16).startswith(b"show"):
                                self._post("tray_show")
                        except OSError:
                            pass
            finally:
                server.close()

        threading.Thread(target=serve, daemon=True).start()

    def _append_event(self, text: str) -> None:
        self.event_list.insert("end", text)
        while self.event_list.size() > 100:
            self.event_list.delete(0)
        self.event_list.see("end")

    def _clear_history(self) -> None:
        self.event_list.delete(0, "end")

    def _tick(self) -> None:
        try:
            elapsed = int(time.time() - self._start_time)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            self.uptime_var.set(f"运行时间 {h:02d}:{m:02d}:{s:02d}")
            self._check_hide_to_tray()
            self._tick_job = self.root.after(1000, self._tick)
        except tk.TclError:
            pass

    # ---------------- 后台监听线程 ----------------

    def _start_watcher(self) -> None:
        self.state = engine.State(self.cfg["state_file"])
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        def on_status(text: str) -> None:
            self._post("status", text=text)
            if text.startswith("连接异常"):
                if self.online:
                    self._post("error", text=text)
                self.online = False
            elif text.startswith(("已登录", "监听中")):
                self.online = True

        def on_message(mail, uid, msg) -> None:
            self._on_message(mail, uid, msg)

        engine.watch_forever(
            self.cfg,
            self.state,
            on_message=on_message,
            catchup=self.catchup,
            on_status=on_status,
        )

    def _on_message(self, mail, uid, msg) -> None:
        info = engine.analyze_message(msg, self.cfg.get("extra_patterns", []))
        if not info["codes"]:
            log.info("[UID %s] 未发现验证码（主题: %s）", uid, info["subject"][:40] or "(无主题)")
            return
        platform = detect_platform(info["subject"], info["from_header"], self.cfg)
        log.info("[UID %s] %s 验证码: %s", uid, platform, info["codes"][0])
        try:
            engine.notify()
        except Exception:
            pass
        self._post(
            "code",
            platform=platform,
            code=info["codes"][0],
            codes=info["codes"],
            subject=info["subject"],
            from_header=info["from_header"],
        )

    # ---------------- 主线程事件泵 ----------------

    def _post(self, kind: str, **payload) -> None:
        self.events.put({"kind": kind, **payload})

    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                try:
                    self._handle_event(event)
                except Exception:
                    log.exception("处理界面事件失败: kind=%s", event.get("kind"))
        except queue.Empty:
            pass
        finally:
            try:
                self._pump_job = self.root.after(200, self._pump)
            except tk.TclError:
                pass

    def _handle_event(self, event: dict) -> None:
        kind = event.pop("kind")
        if kind == "status":
            text = event["text"]
            self.status_var.set(text)
            self._update_status_dot(text)
            if text.startswith(("已登录", "监听中", "连接异常")):
                self._append_event(f"{time.strftime('%H:%M:%S')}  {text}")
        elif kind == "code":
            self._append_event(
                f"{time.strftime('%H:%M:%S')}  [{event['platform']}] "
                f"验证码 {event['code']}"
            )
            self.pending.append(event)
            self._show_next()
        elif kind == "error":
            self._show_error(event["text"])
        elif kind == "tray_show":
            self._restore_window()
        elif kind == "tray_quit":
            self._tray_stop()
            self.quit()
        elif kind == "reconfigure":
            self._request_reconfigure()

    def _update_status_dot(self, text: str) -> None:
        if text.startswith("连接异常"):
            color = "#EF4444"
        elif text.startswith(("已登录", "监听中")):
            color = "#22C55E"
        else:
            color = "#F59E0B"
        self.dot_label.config(fg=color)

    def _show_next(self) -> None:
        if self.current is None and self.pending:
            info = self.pending.pop(0)
            try:
                self.current = CodePopup(self, info)
                log.info("弹窗已显示: %s 验证码 %s", info["platform"], info["code"])
            except Exception:
                log.exception("弹窗创建失败: %s", info)
                self.current = None

    def on_popup_closed(self) -> None:
        self.current = None
        self._show_next()

    def _show_error(self, text: str) -> None:
        # 非阻塞提示：只记录到事件列表，不弹模态框，避免卡住后续验证码弹窗
        self._append_event(f"{time.strftime('%H:%M:%S')}  ⚠ {text}")
        log.error("%s", text)

    def quit(self) -> None:
        self._tray_stop()
        for job in (self._pump_job, self._tick_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

_LOGGING_READY = False


def _setup_logging() -> None:
    """日志：始终写一份到 tool.log（pythonw/exe 无控制台时也能排查），有控制台时同时输出。"""
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    _LOGGING_READY = True

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        file_handler = logging.FileHandler(engine.BASE_DIR / "tool.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        root_logger.addHandler(file_handler)
    except Exception:
        pass
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        root_logger.addHandler(stream_handler)


def run_gui(cfg: dict, catchup: bool = False) -> str:
    """启动监听窗口并进入消息循环；返回退出原因：'quit' 或 'reconfigure'。"""
    _setup_logging()
    if not _acquire_single_instance():
        # 已有实例在运行：唤起它的窗口，本实例直接退出
        _signal_existing_instance()
        print("检测到工具已在运行，已切换到现有窗口（本实例退出）", flush=True)
        return "quit"

    root = tk.Tk()
    app = App(root, cfg, catchup=catchup)
    root.mainloop()
    return app.exit_reason


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ 邮箱验证码弹窗工具（IMAP IDLE 推送）")
    parser.add_argument(
        "-c",
        "--config",
        default=str(engine.DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {engine.DEFAULT_CONFIG_PATH.name}）",
    )
    parser.add_argument("--catchup", action="store_true", help="启动时也处理存量未处理邮件")
    args = parser.parse_args()

    try:
        cfg = engine.load_config(Path(args.config))
    except SystemExit as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("配置错误", str(exc), parent=root)
        return
    run_gui(cfg, catchup=args.catchup)


if __name__ == "__main__":
    main()
