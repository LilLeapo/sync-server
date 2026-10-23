"""测试用假 Chrome：在 fd 3 读、fd 4 写 NUL 分隔的 CDP JSON，表现由 FAKE_CHROME_MODE 决定。

    ok         正常应答，截图返回 1×1 PNG；FAKE_CHROME_DELAY 秒后才回截图
    hang       什么都不回，管道关了也不退出
    crash      起一个比自己活得久的子进程（pid 记进日志的 child），往 stderr 写一行后直接退出
    cdp_error  Page.setDocumentContent 返回 CDP 错误
    notpng     截图返回的不是 PNG
    events     每个应答前先插一条事件和一条别的 id 的消息
    wide       元素里的内容比元素宽

FAKE_CHROME_LOG 指定时，每个进程往里追加一行 JSON：pid、argv、起止时间。
"""
import base64
import json
import os
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_CHROME_MODE", "ok")
LOG = os.environ.get("FAKE_CHROME_LOG")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
STARTED = time.time()


def log(**extra):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:], "start": STARTED, **extra}) + "\n")


def send(msg):
    os.write(4, json.dumps(msg).encode("utf-8") + b"\0")


def result(method):
    if method == "Target.createTarget":
        return {"targetId": "T1"}
    if method == "Target.attachToTarget":
        return {"sessionId": "S1"}
    if method == "Page.getFrameTree":
        return {"frameTree": {"frame": {"id": "F1"}}}
    if method == "Runtime.evaluate":
        return {"result": {"type": "object", "value": [8, 8, 320, 180, 156 if MODE == "wide" else 0]}}
    if method == "Page.captureScreenshot":
        time.sleep(float(os.environ.get("FAKE_CHROME_DELAY") or 0))
        return {"data": base64.b64encode(b"GIF89a" if MODE == "notpng" else PNG).decode()}
    return {}


def answer(msg):
    method = msg.get("method")
    if method == "Browser.close":
        log(end=time.time())
        sys.exit(0)
    if MODE == "events":
        send({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "T9"}}})
        send({"id": msg["id"] + 1000, "result": {"data": "not yours"}})
    if MODE == "cdp_error" and method == "Page.setDocumentContent":
        send({"id": msg["id"], "error": {"code": -32000, "message": "fake failure"}})
        return
    send({"id": msg["id"], "result": result(method), **({"sessionId": msg["sessionId"]} if "sessionId" in msg else {})})


def main():
    log()
    if MODE == "crash":
        log(child=subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]).pid)
        sys.stderr.write("[fake] error while loading shared libraries: libnss3.so\n")
        sys.exit(3)
    if MODE == "hang":
        while True:
            time.sleep(60)
    buf = b""
    while True:
        chunk = os.read(3, 1 << 16)
        if not chunk:
            log(end=time.time())
            return
        buf += chunk
        while b"\0" in buf:
            raw, _, buf = buf.partition(b"\0")
            answer(json.loads(raw))


if __name__ == "__main__":
    main()
