#!/usr/bin/env python3
"""无头 Chrome 截图：每次调用起一个独立 Chrome，经 --remote-debugging-pipe（fd 3/4）说 CDP，截完即关。

    render = HeadlessChrome(find_chrome())
    png = render(html, "card")      # 按 #card 的位置裁剪，返回 PNG 字节；失败抛 RenderError

用管道而不用端口：不开 TCP 端口、不用轮询 DevToolsActivePort、不需要 WebSocket 客户端；
管道一断 Chrome 自己退出，服务被 SIGKILL 也不会留下孤儿浏览器。本模块不知道「卡片」是什么。
"""
from __future__ import annotations

import base64
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time

PROFILE_PREFIX = "card-chrome-"
MAC_APPS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium")
LINUX_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
FLAGS = ("--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
         "--disable-extensions", "--disable-background-networking", "--disable-component-update", "--disable-sync",
         "--mute-audio", "--host-resolver-rules=MAP * ~NOTFOUND", "--remote-debugging-pipe")
VIEWPORT_WIDTH = 1200
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 管道作为 sh 的 stdin/stdout 交给子进程，再由 sh 挪到 Chrome 约定的 3/4：
# 不用 preexec_fn（多线程下不安全），fd 号都是个位数（dash 不认 ≥10 的 fd）
REMAP = 'exec "$0" "$@" 3<&0 4>&1 0</dev/null 1>/dev/null'


class RenderError(RuntimeError):
    """启动失败、CDP 报错、超时、尺寸异常或截图不是 PNG。消息是简短中文原因加 Chrome stderr 末尾。"""


def find_chrome(configured: str = "", which=shutil.which) -> str | None:
    """配置了路径就只认它（不可执行返回 None，不悄悄换别的）；否则找 macOS 的应用，再按名字找 chromium 等。"""
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    for path in MAC_APPS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in LINUX_NAMES:
        found = which(name)
        if found:
            return found
    return None


class HeadlessChrome:
    """render(html, element_id) → PNG 字节。可多线程调用，同时最多 max_parallel 个 Chrome。"""

    def __init__(self, binary: str, *, timeout_s: float = 20.0, scale: float = 2.0, max_parallel: int = 2,
                 max_height_css: int = 6000, euid=os.geteuid):
        self.binary = binary
        self.timeout_s = timeout_s
        self.scale = scale
        self.max_height_css = max_height_css
        self._euid = euid
        self._slots = threading.BoundedSemaphore(max_parallel)

    def __call__(self, html: str, element_id: str) -> bytes:
        deadline = time.monotonic() + self.timeout_s  # 排队、启动、截图共用一个期限
        if not self._slots.acquire(timeout=self.timeout_s):
            raise RenderError("制图繁忙，请稍后再试")
        try:
            profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
            try:
                return self._render(profile, html, element_id, deadline)
            finally:
                shutil.rmtree(profile, ignore_errors=True)
        finally:
            self._slots.release()

    def _command(self, profile: str) -> list[str]:
        sandbox = ["--no-sandbox"] if self._euid() == 0 else []  # root 下 Chrome 不开沙箱就拒绝启动
        return [self.binary, *FLAGS, f"--user-data-dir={profile}", *sandbox, "about:blank"]

    def _render(self, profile: str, html: str, element_id: str, deadline: float) -> bytes:
        log = os.path.join(profile, "stderr.log")
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        try:
            with open(log, "wb") as err:
                proc = subprocess.Popen(["/bin/sh", "-c", REMAP, *self._command(profile)],
                                        stdin=to_r, stdout=from_w, stderr=err, start_new_session=True)
        except OSError as e:
            for fd in (to_r, to_w, from_r, from_w):
                os.close(fd)
            raise RenderError(f"启动 Chrome 失败：{e}") from e
        os.close(to_r)
        os.close(from_w)
        os.set_blocking(to_w, False)
        png, failure = None, None
        try:
            png = _capture(_Pipe(to_w, from_r, deadline), html, element_id, self.scale, self.max_height_css)
        except (OSError, ValueError, LookupError, TypeError, RenderError) as e:  # TimeoutError 属于 OSError
            failure = e
        finally:
            os.close(to_w)  # fd 3 读到 EOF，Chrome 自己退出
            os.close(from_r)
            _reap(proc, grace=3.0 if failure is None else 0.0)
        if failure is not None:
            reason = (f"超过 {self.timeout_s:g} 秒没有完成" if isinstance(failure, TimeoutError) else
                      str(failure) if isinstance(failure, RenderError) else f"Chrome 通信异常：{failure!r}")
            raise RenderError(reason + _tail(log)) from failure
        return png


def _capture(cdp: "_Pipe", html: str, element_id: str, scale: float, max_height_css: int) -> bytes:
    target = cdp.call("Target.createTarget", url="about:blank")["targetId"]
    sid = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    cdp.call("Emulation.setDeviceMetricsOverride", sid, width=VIEWPORT_WIDTH, height=600,
             deviceScaleFactor=scale, mobile=False)
    frame = cdp.call("Page.getFrameTree", sid)["frameTree"]["frame"]["id"]
    cdp.call("Page.setDocumentContent", sid, frameId=frame, html=html)
    found = cdp.call("Runtime.evaluate", sid, awaitPromise=True, returnByValue=True, expression=(
        f"document.fonts.ready.then(() => {{ const r = document.getElementById({json.dumps(element_id)})"
        ".getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; })"))
    if "exceptionDetails" in found:
        detail = (found["exceptionDetails"].get("exception") or {}).get("description") or ""
        raise RenderError(f"取不到 #{element_id} 的位置：{detail[:120]}")
    x, y, w, h = found["result"]["value"]
    if not (0 < w <= VIEWPORT_WIDTH and 0 < h <= max_height_css):
        raise RenderError(f"卡片尺寸异常（{w:g}×{h:g}）")
    shot = cdp.call("Page.captureScreenshot", sid, format="png", captureBeyondViewport=True,
                    clip={"x": x, "y": y, "width": w, "height": h, "scale": 1})
    png = base64.b64decode(shot["data"])
    if not png.startswith(PNG_MAGIC):
        raise RenderError("截图不是 PNG")
    cdp.send("Browser.close")
    return png


class _Pipe:
    """CDP over --remote-debugging-pipe：每条消息是一段 JSON 加 b"\\0"；读写都不越过 deadline。"""

    def __init__(self, write_fd: int, read_fd: int, deadline: float):
        self.write_fd = write_fd
        self.read_fd = read_fd
        self.deadline = deadline
        self.seq = 0
        self.buf = bytearray()

    def send(self, method: str, session: str | None = None, **params) -> int:
        self.seq += 1
        msg = {"id": self.seq, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        data = memoryview(json.dumps(msg).encode("utf-8") + b"\0")
        while data:
            self._wait(self.write_fd, writing=True)
            try:
                data = data[os.write(self.write_fd, data):]
            except BlockingIOError:
                pass
        return self.seq

    def call(self, method: str, session: str | None = None, **params) -> dict:
        seq = self.send(method, session, **params)
        while True:
            reply = self._read()
            if reply.get("id") != seq:  # 事件
                continue
            if "error" in reply:
                raise RenderError(f"CDP {method} 出错：{(reply['error'] or {}).get('message')}")
            return reply.get("result") or {}

    def _read(self) -> dict:
        while True:
            end = self.buf.find(b"\0")
            if end >= 0:
                raw = bytes(self.buf[:end])
                del self.buf[:end + 1]
                return json.loads(raw)
            self._wait(self.read_fd, writing=False)
            chunk = os.read(self.read_fd, 1 << 16)
            if not chunk:
                raise RenderError("Chrome 意外退出")
            self.buf += chunk

    def _wait(self, fd: int, writing: bool):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        readable, writable, _ = select.select([] if writing else [fd], [fd] if writing else [], [], left)
        if not (readable or writable):
            raise TimeoutError


def _reap(proc: subprocess.Popen, grace: float):
    """等 Chrome 自己退出；超过 grace 秒就整组 TERM，再等 1 秒 KILL。"""
    for sig, wait in ((None, grace), (signal.SIGTERM, 1.0), (signal.SIGKILL, None)):
        if sig is not None:
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _tail(log: str, limit: int = 300) -> str:
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit * 4))
            text = f.read().decode("utf-8", "replace").strip()[-limit:]
    except OSError:
        return ""
    return f"；chrome: {text}" if text else ""


def sweep_profiles(max_age_s: float = 3600) -> int:
    """删掉临时目录里超过 max_age_s 的 card-chrome-* 目录（服务被杀时没来得及清理的）。"""
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        entries = list(os.scandir(tempfile.gettempdir()))
    except OSError:
        return 0
    for entry in entries:
        try:
            stale = (entry.name.startswith(PROFILE_PREFIX) and entry.is_dir(follow_symlinks=False)
                     and entry.stat(follow_symlinks=False).st_mtime < cutoff)
        except OSError:
            continue
        if stale:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed
