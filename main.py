#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 邮箱验证码 -> 剪贴板 实时同步工具

原理:
  1. 通过 IMAP (imap.qq.com:993) 登录 QQ 邮箱，使用授权码（非 QQ 密码）
  2. 进入 IDLE 推送模式，服务器有新邮件时立即收到 EXISTS 通知（秒级）
  3. 解析新邮件正文，用正则提取验证码
  4. 将验证码复制到系统剪贴板并响铃提示

用法:
  python main.py --check       # 检查配置、登录、IDLE 支持
  python main.py               # 开始实时监听
  python main.py --catchup     # 启动时也处理存量未处理邮件
"""

from __future__ import annotations

import argparse
import email
import email.header
import html
import imaplib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import pyperclip
except ImportError:
    pyperclip = None

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_STATE_PATH = BASE_DIR / ".state.json"

log = logging.getLogger("qq-code-sync")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    """读取 config.json，环境变量优先。"""
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"配置文件 {path} 解析失败: {exc}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"配置文件 {path} 顶层必须是 JSON 对象")

    env = os.environ
    merged = {
        "email": env.get("QQ_IMAP_EMAIL") or str(cfg.get("email", "")).strip(),
        "auth_code": env.get("QQ_IMAP_AUTH_CODE") or str(cfg.get("auth_code", "")).strip(),
        "host": env.get("QQ_IMAP_HOST") or str(cfg.get("host", "imap.qq.com")),
        "port": int(env.get("QQ_IMAP_PORT") or cfg.get("port", 993)),
        "poll_interval": max(5.0, float(cfg.get("poll_interval", 30))),
        "idle_duration": max(60.0, float(cfg.get("idle_duration", 25 * 60))),
        "mark_seen": bool(cfg.get("mark_seen", False)),
        "state_file": str(cfg.get("state_file", DEFAULT_STATE_PATH)),
        "extra_patterns": [str(p) for p in cfg.get("extra_patterns", [])],
    }
    if not merged["email"]:
        raise SystemExit(
            "缺少邮箱地址：请在 config.json 填写 email，或设置环境变量 QQ_IMAP_EMAIL"
        )
    if not merged["auth_code"]:
        raise SystemExit(
            "缺少授权码：请在 config.json 填写 auth_code，或设置环境变量 QQ_IMAP_AUTH_CODE"
        )

    state_path = Path(merged["state_file"])
    if not state_path.is_absolute():
        state_path = BASE_DIR / state_path
    merged["state_file"] = str(state_path)
    return merged


# ---------------------------------------------------------------------------
# 已处理进度（按 UID 断点续传，避免重启后重复复制旧验证码）
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: str):
        self.path = Path(path)
        self.last_uid = 0
        self.uidvalidity = None
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_uid = int(data.get("last_uid", 0))
            self.uidvalidity = data.get("uidvalidity")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.warning("状态文件 %s 无法解析，重新开始", self.path)

    def save(self) -> None:
        data = {
            "last_uid": self.last_uid,
            "uidvalidity": self.uidvalidity,
        }
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# IMAP 基础操作
# ---------------------------------------------------------------------------

class QQIMAP4SSL(imaplib.IMAP4_SSL):
    """SSL 连接增强：逐个快速尝试所有解析地址（IPv4/IPv6），避免某个地址族超时卡死。"""

    def _create_socket(self, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        host = None if not self.host else self.host
        sys.audit("imaplib.open", self, self.host, self.port)

        infos = socket.getaddrinfo(host, self.port, 0, socket.SOCK_STREAM)
        last_error = None
        attempt_timeout = timeout if timeout else 15

        for family, socktype, proto, _, addr in infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(attempt_timeout)
                sock.connect(addr)
                wrapped = self.ssl_context.wrap_socket(
                    sock, server_hostname=self.host
                )
                self._connected_addr = addr
                return wrapped
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        if last_error is not None:
            raise last_error
        raise OSError(f"无法解析或连接 {self.host}:{self.port}")


def connect(cfg: dict) -> imaplib.IMAP4_SSL:
    mail = QQIMAP4SSL(cfg["host"], cfg["port"], timeout=8)
    log.info("TCP 连接成功: %s", getattr(mail, "_connected_addr", "?"))
    mail.login(cfg["email"], cfg["auth_code"])
    mail.select("INBOX")
    return mail


def get_mailbox_status(mail: imaplib.IMAP4, mailbox: str = "INBOX"):
    """返回 (UIDNEXT, UIDVALIDITY)。"""
    typ, data = mail.status(mailbox, "(UIDNEXT UIDVALIDITY)")
    if typ != "OK":
        raise imaplib.IMAP4.error(f"STATUS 失败: {data!r}")
    text = data[0].decode(errors="replace")
    uidnext = int(re.search(r"UIDNEXT (\d+)", text).group(1))
    uidvalidity = int(re.search(r"UIDVALIDITY (\d+)", text).group(1))
    return uidnext, uidvalidity


def initialize_state(mail: imaplib.IMAP4, state: State, catchup: bool) -> None:
    uidnext, uidvalidity = get_mailbox_status(mail)

    if state.uidvalidity is not None and state.uidvalidity != uidvalidity:
        log.warning("邮箱 UIDVALIDITY 已变化，重置处理进度")
        state.uidvalidity = uidvalidity
        state.last_uid = uidnext - 1
        state.save()
        return

    if state.uidvalidity is None:
        state.uidvalidity = uidvalidity
        if catchup:
            # 首次运行且要处理存量：只看最近 100 封，避免一次性拉整个邮箱
            state.last_uid = max(0, uidnext - 1 - 100)
            log.info("首次运行（--catchup）：处理最近 100 封邮件中的验证码")
        else:
            state.last_uid = uidnext - 1
            log.info("首次运行：只处理之后到达的新邮件（可用 --catchup 处理存量）")
        state.save()


def fetch_one(mail: imaplib.IMAP4, uid: str):
    """按 UID 拉取一封邮件（BODY.PEEK 不改变已读状态）。"""
    typ, data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK":
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and item[1]:
            return email.message_from_bytes(item[1])
    return None


def fetch_new(mail: imaplib.IMAP4, state: State):
    """返回 (uid, message) 列表，并推进进度。"""
    uidnext, uidvalidity = get_mailbox_status(mail)
    if state.uidvalidity != uidvalidity:
        raise imaplib.IMAP4.error("UIDVALIDITY 已变化，需要重新初始化")

    highest = uidnext - 1
    start = state.last_uid + 1
    if start > highest:
        return []

    # 注意：QQ 服务器要求显式写 UID 关键字，否则按"邮件序号"解析，
    # 会因序号大于总邮件数而返回空，导致新邮件被跳过
    typ, data = mail.uid("SEARCH", None, "UID", f"{start}:*")
    if typ != "OK":
        raise imaplib.IMAP4.error(f"UID SEARCH 失败: {data!r}")
    uids = (data[0] or b"").split() if data else []
    if not uids:
        state.last_uid = max(state.last_uid, highest)
        state.save()
        return []

    messages = []
    last_ok = state.last_uid
    for uid in uids:
        uid_s = uid.decode("ascii")
        msg = fetch_one(mail, uid_s)
        if msg is None:
            log.warning("无法读取邮件 UID=%s，留待下次重试", uid_s)
            break
        messages.append((uid_s, msg))
        last_ok = int(uid_s)

    if last_ok > state.last_uid:
        state.last_uid = last_ok
        state.save()
    return messages


def mark_seen(mail: imaplib.IMAP4, uid: str) -> None:
    try:
        mail.uid("STORE", uid, "+FLAGS", r"(\Seen)")
    except Exception as exc:
        log.warning("标记已读失败 UID=%s: %s", uid, exc)


# ---------------------------------------------------------------------------
# 邮件正文解析
# ---------------------------------------------------------------------------

def decode_bytes(raw: bytes, charset: str | None) -> str:
    if charset and charset.lower() == "unknown-8bit":
        charset = None
    candidates = [charset, "utf-8", "gb18030", "gbk", "big5"]
    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def html_to_text(html_text: str) -> str:
    # 先去掉样式/脚本块，避免 CSS 颜色值（如 #424245）被当成验证码
    text = re.sub(
        r"<style[^>]*>.*?</style>", " ", html_text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r"<script[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|td|h[1-6]|li)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def get_body_text(msg: email.message.Message) -> str:
    parts = []

    def collect(part: email.message.Message) -> None:
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            return
        text = decode_bytes(payload, part.get_content_charset())
        if ctype == "text/plain":
            parts.append(text)
        elif ctype == "text/html":
            parts.append(html_to_text(text))

    if msg.is_multipart():
        for part in msg.walk():
            collect(part)
    else:
        collect(msg)
    return "\n".join(parts)


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    out = []
    for raw, charset in email.header.decode_header(value):
        if isinstance(raw, bytes):
            enc = charset if charset and charset.lower() != "unknown-8bit" else "utf-8"
            out.append(raw.decode(enc, errors="replace"))
        else:
            out.append(raw)
    return "".join(out)


# ---------------------------------------------------------------------------
# 验证码提取
# ---------------------------------------------------------------------------

KEYWORD_PATTERNS = [
    re.compile(
        r"验证码|校验码|动态密码|安全码|短信验证码|登录验证码|安全验证码"
        r"|一次性密码|一次性代码|确认码"
    ),
    re.compile(
        r"verification code|security code|one[\s-]?time code"
        r"|auth(?:entication)? code|otp|2fa",
        re.IGNORECASE,
    ),
]


def normalize_text(text: str) -> str:
    text = html.unescape(text)
    # 去掉 CSS 十六进制颜色（#424245 之类），避免被当成验证码
    text = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", text)
    # HTML 中验证码可能被拆成 <span>1</span><span>2</span>，去掉数字间的空白
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    return re.sub(r"\s+", " ", text)


def extract_codes(text: str, extra_patterns=()) -> list[str]:
    """按可信度返回候选验证码：关键词后的数字码优先，无关键词时保守兜底。"""
    text = normalize_text(text)
    found: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        if code not in seen:
            seen.add(code)
            found.append(code)

    has_keyword = any(p.search(text) for p in KEYWORD_PATTERNS)

    for pattern in KEYWORD_PATTERNS:
        for match in pattern.finditer(text):
            # 关键词后面 80 个字符内，优先取第一串纯数字
            tail = text[match.end() : match.end() + 80]
            digit = re.search(r"\b(\d{4,8})\b", tail)
            if digit:
                add(digit.group(1))
                continue
            # 退而求其次：字母数字组合（至少含 2 位数字），避免抓平台名/普通单词
            alnum = re.search(r"\b([A-Za-z0-9]{4,10})\b", tail)
            if alnum:
                code = alnum.group(1)
                if sum(ch.isdigit() for ch in code) >= 2:
                    add(code)

    # 兜底：只有邮件里确实出现验证码类关键词时，才认独立的 6 位数字
    if not found and has_keyword:
        for match in re.finditer(r"\b\d{6}\b", text):
            add(match.group(0))
            if len(found) >= 5:
                break

    for raw in extra_patterns:
        try:
            pattern = re.compile(raw)
        except re.error:
            log.warning("忽略无效自定义正则: %s", raw)
            continue
        for match in pattern.finditer(text):
            code = match.group(1) if match.lastindex else match.group(0)
            add(code)

    return found[:10]


# ---------------------------------------------------------------------------
# 剪贴板
# ---------------------------------------------------------------------------

def _win32_copy_native(text: str) -> None:
    """Windows 原生剪贴板写入（ctypes 直调，毫秒级，不需要外部进程）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    data = text.encode("utf-16-le") + b"\x00\x00"

    if not user32.OpenClipboard(None):
        raise OSError("无法打开剪贴板")
    try:
        user32.EmptyClipboard()
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            raise OSError("剪贴板内存分配失败")
        try:
            ptr = kernel32.GlobalLock(h_mem)
            if not ptr:
                raise OSError("剪贴板内存锁定失败")
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(h_mem)
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                raise OSError("写入剪贴板失败")
            h_mem = None  # 成功后内存归系统管理
        finally:
            if h_mem:
                kernel32.GlobalFree(h_mem)
    finally:
        user32.CloseClipboard()


def copy_to_clipboard(text: str) -> str:
    """复制到剪贴板，返回使用的后端名。"""
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
            return "pyperclip"
        except Exception as exc:
            log.warning("pyperclip 复制失败（%s），改用系统命令", exc)

    if sys.platform == "win32":
        # 方案一：原生 ctypes 直写剪贴板（毫秒级）
        try:
            _win32_copy_native(text)
            return "ctypes 原生"
        except Exception as exc:
            log.warning("原生剪贴板写入失败（%s），改用系统命令", exc)
        # 方案二：clip.exe（UTF-16LE + BOM，支持中文）
        try:
            data = b"\xff\xfe" + text.encode("utf-16-le")
            subprocess.run(
                ["clip"],
                input=data,
                check=False,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "clip.exe"
        except Exception:
            pass
        # 方案三：PowerShell Set-Clipboard
        try:
            script = (
                "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
                "$t = [Console]::In.ReadToEnd(); Set-Clipboard -Value $t"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                input=text.encode("utf-8"),
                check=False,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "PowerShell Set-Clipboard"
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                check=False,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "pbcopy"
        except Exception:
            pass
    else:
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            try:
                subprocess.run(
                    cmd,
                    input=text.encode("utf-8"),
                    check=False,
                    timeout=10,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return cmd[0]
            except Exception:
                continue
    raise RuntimeError("没有可用的剪贴板后端，请安装 pyperclip（pip install pyperclip）")


def detect_clipboard_backend() -> str:
    if pyperclip is not None:
        return "pyperclip"
    if sys.platform == "win32":
        return "clip.exe / PowerShell Set-Clipboard"
    if sys.platform == "darwin":
        return "pbcopy"
    for exe in ("wl-copy", "xclip", "xsel"):
        if shutil.which(exe):
            return exe
    return "无可用后端（建议 pip install pyperclip）"


def notify() -> None:
    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_OK)
            return
        except Exception:
            pass
    write = getattr(sys.stdout, "write", None)
    flush = getattr(sys.stdout, "flush", None)
    if write is not None:
        try:
            write("\a")
            if flush is not None:
                flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 邮件处理
# ---------------------------------------------------------------------------

def analyze_message(msg: email.message.Message, extra_patterns=()) -> dict:
    """解析邮件，返回 GUI/CLI 共用的邮件信息。"""
    subject = decode_header_value(msg.get("Subject", ""))
    from_header = decode_header_value(msg.get("From", ""))
    body = get_body_text(msg)
    text = f"{subject}\n{body}"
    codes = extract_codes(text, extra_patterns)
    return {
        "subject": subject,
        "from_header": from_header,
        "body": body,
        "codes": codes,
    }


def process_message(mail: imaplib.IMAP4, uid: str, msg: email.message.Message, cfg: dict) -> bool:
    info = analyze_message(msg, cfg.get("extra_patterns", []))
    subject, codes = info["subject"], info["codes"]

    if not codes:
        log.info("[UID %s] 未发现验证码（主题: %s）", uid, subject[:60] or "(无主题)")
        return False

    code = codes[0]
    backend = copy_to_clipboard(code)
    notify()
    log.info("=" * 44)
    log.info("检测到验证码: %s", code)
    log.info("来源邮件: %s", subject[:80] or "(无主题)")
    if len(codes) > 1:
        log.info("其他候选: %s", "、".join(codes[1:]))
    log.info("已通过 %s 复制到剪贴板", backend)
    log.info("=" * 44)

    if cfg.get("mark_seen"):
        mark_seen(mail, uid)
    return True


def process_new_mail(
    mail: imaplib.IMAP4,
    state: State,
    cfg: dict,
    on_message=None,
) -> None:
    """处理新邮件；on_message(mail, uid, msg) 存在时由它接管每封邮件的处理。"""
    messages = fetch_new(mail, state)
    if not messages:
        return
    log.info("收到 %d 封新邮件", len(messages))
    for uid, msg in messages:
        try:
            if on_message is not None:
                on_message(mail, uid, msg)
            else:
                process_message(mail, uid, msg, cfg)
        except Exception as exc:
            log.warning("处理邮件 UID=%s 出错: %s", uid, exc)


# ---------------------------------------------------------------------------
# 监听循环
# ---------------------------------------------------------------------------

def builtin_idle_available() -> bool:
    """Python 3.14+ 内置 Idler 上下文管理器。"""
    return hasattr(imaplib, "Idler")


def idle_loop(mail: imaplib.IMAP4, state: State, cfg: dict, on_message=None) -> None:
    """IDLE 推送循环：阻塞等待新邮件通知，收到后处理并重新进入 IDLE。"""
    duration = cfg["idle_duration"]
    while True:
        try:
            with mail.idle(duration=duration) as idler:
                for typ, data in idler:
                    log.debug("IDLE 响应: %s %r", typ, data)
                    if typ in ("EXISTS", "RECENT"):
                        break
        except imaplib.IMAP4.error as exc:
            log.warning("IDLE 不可用（%s），改用轮询模式", exc)
            poll_loop(mail, state, cfg, on_message)
            return
        process_new_mail(mail, state, cfg, on_message)


def poll_loop(mail: imaplib.IMAP4, state: State, cfg: dict, on_message=None) -> None:
    """兜底轮询模式。"""
    while True:
        time.sleep(cfg["poll_interval"])
        process_new_mail(mail, state, cfg, on_message)


def watch_forever(
    cfg: dict,
    state: State,
    on_message=None,
    poll_only: bool = False,
    catchup: bool = False,
    on_status=None,
) -> None:
    """带自动重连的监听主循环：优先 IDLE 推送，不可用时降级轮询。"""
    backoff = 5
    while True:
        mail = None
        try:
            if on_status:
                on_status("正在连接 ...")
            log.info("连接 %s:%s ...", cfg["host"], cfg["port"])
            mail = connect(cfg)
            log.info("登录成功: %s", cfg["email"])
            if on_status:
                on_status(f"已登录 {cfg['email']}")

            initialize_state(mail, state, catchup=catchup)
            process_new_mail(mail, state, cfg, on_message)

            if poll_only or not builtin_idle_available():
                if on_status:
                    on_status(f"监听中（轮询 {cfg['poll_interval']:.0f} 秒）")
                log.info("使用轮询模式（每 %.0f 秒检查一次）", cfg["poll_interval"])
                poll_loop(mail, state, cfg, on_message)
            else:
                if on_status:
                    on_status("监听中（IDLE 推送）")
                log.info("进入 IDLE 推送模式，等待新邮件（Ctrl+C 退出）")
                idle_loop(mail, state, cfg, on_message)

        except KeyboardInterrupt:
            log.info("用户中断，退出")
            return
        except Exception as exc:
            log.warning("异常: %s（%.0f 秒后重连）", exc, backoff)
            if on_status:
                on_status(f"连接异常，{backoff:.0f} 秒后重连")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 5
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 检查 / 自检
# ---------------------------------------------------------------------------

def do_check(mail: imaplib.IMAP4, cfg: dict) -> None:
    log.info("服务器: %s:%s", cfg["host"], cfg["port"])
    log.info("账号: %s", cfg["email"])
    log.info("登录: 成功")
    typ, data = mail.select("INBOX")
    total = data[0].decode() if data and data[0] else "?"
    log.info("收件箱邮件数: %s", total)
    uidnext, uidvalidity = get_mailbox_status(mail)
    log.info("UIDNEXT=%s UIDVALIDITY=%s", uidnext, uidvalidity)

    caps = [
        c.decode(errors="replace") if isinstance(c, bytes) else c
        for c in getattr(mail, "capabilities", ())
    ]
    if "IDLE" in caps:
        log.info("IMAP IDLE: 服务器支持（实时推送可用）")
    else:
        log.info("IMAP IDLE: 服务器未声明支持（将使用轮询模式）")
    if not builtin_idle_available():
        log.info("IMAP IDLE: 当前 Python 版本无内置 Idler（将使用轮询模式，建议 Python 3.14+）")
    log.info("剪贴板后端: %s", detect_clipboard_backend())
    log.info("检查完成，可以运行 python gui.py 启动弹窗版")


SELFTEST_SAMPLES = [
    ("QQ 验证码", "【QQ】您的验证码为：123456，10分钟内有效。如非本人操作请忽略。"),
    ("空格分隔", "您的验证码 654321 请勿泄露给他人。"),
    ("英文邮件", "Your verification code is 789012. It expires in 10 minutes."),
    ("无分隔", "登录验证码246810"),
    ("HTML 拆字", "<div>验证码：<span>1</span><span>3</span><span>5</span><span>7</span><span>9</span><span>0</span></div>"),
    ("动态密码", "动态密码：112233"),
    ("平台名在前", "GLaDOS Authentication Code\nGLaDOS Verification Code: 804133 Enter this code."),
    ("一次性代码", "你的一次性代码为: 574738 仅在官方网站输入。隐私声明: https://go.microsoft.com/fwlink/?LinkId=521839"),
    ("不应误报", "验证码已发送到您的手机，请查收。"),
    ("无关键词数字", "DeepSeek API 计费调整预告，2026 年生效。"),
    ("Discord 提及", "j9052在Pika 中提及了您 频道内消息 060607"),
]


def run_selftest() -> int:
    expected = {"123456", "654321", "789012", "246810", "135790", "112233", "804133", "574738"}
    all_codes: set[str] = set()
    ok = True

    for name, raw in SELFTEST_SAMPLES:
        text = html_to_text(raw) if raw.lstrip().startswith("<") else raw
        codes = extract_codes(text)
        all_codes.update(codes)
        log.info("%-12s -> %s", name, "、".join(codes) if codes else "(未找到)")

    missing = sorted(expected - all_codes)
    if missing:
        ok = False
        log.error("未提取到: %s", "、".join(missing))

    for name, raw in SELFTEST_SAMPLES:
        if name in ("不应误报", "无关键词数字", "Discord 提及"):
            text = html_to_text(raw) if raw.lstrip().startswith("<") else raw
            if extract_codes(text):
                ok = False
                log.error("“%s”被误判为验证码: %s", name, extract_codes(text))

    log.info("剪贴板后端: %s", detect_clipboard_backend())
    log.info("自检%s", "通过 ✔" if ok else "失败 ✘")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(cfg: dict, args: argparse.Namespace) -> None:
    state = State(cfg["state_file"])

    if args.check:
        mail = connect(cfg)
        try:
            do_check(mail, cfg)
        finally:
            try:
                mail.logout()
            except Exception:
                pass
        return

    if args.once:
        mail = connect(cfg)
        try:
            initialize_state(mail, state, catchup=args.catchup)
            process_new_mail(mail, state, cfg)
        finally:
            try:
                mail.logout()
            except Exception:
                pass
        log.info("--once：本轮处理完成，退出")
        return

    def handler(mail, uid, msg):
        process_message(mail, uid, msg, cfg)

    watch_forever(
        cfg,
        state,
        on_message=handler,
        poll_only=args.poll_only,
        catchup=args.catchup,
    )


def ensure_console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    ensure_console_utf8()

    parser = argparse.ArgumentParser(
        description="QQ 邮箱验证码实时同步到剪贴板（IMAP IDLE 推送）"
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH.name}）",
    )
    parser.add_argument("--check", action="store_true", help="检查配置、登录和 IDLE 支持后退出")
    parser.add_argument("--catchup", action="store_true", help="启动时也处理之前未处理的新邮件")
    parser.add_argument("--poll-only", action="store_true", help="强制使用轮询模式，不用 IDLE")
    parser.add_argument("--once", action="store_true", help="处理完当前新邮件后退出")
    parser.add_argument("--selftest", action="store_true", help="运行验证码提取自检")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.selftest:
        raise SystemExit(run_selftest())

    cfg = load_config(Path(args.config))
    run(cfg, args)


if __name__ == "__main__":
    main()
