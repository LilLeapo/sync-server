"""消息存储：jsonl 落盘、图片保存、wxgf 解码、60s 去重窗口。

从 WABTest viewer 迁移。依赖 ffmpeg（仅用于解码微信 wxgf 私有图片格式）。
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request

MAX_IMAGE_BYTES = 25 * 1024 * 1024
LEGACY_LOG = "/tmp/chat_forward.log"

EXT_FROM_PATH = re.compile(r"\.([A-Za-z0-9]+)$")


def now_ms():
    return int(time.time() * 1000)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def device_id_of(msg: dict | None) -> str:
    """消息携带的设备标识；旧数据/旧模块没有该字段时为空串。"""
    return str((msg or {}).get("deviceId") or "").strip()


def guess_ext(path: str | None, default: str = "jpg") -> str:
    if path:
        m = EXT_FROM_PATH.search(path)
        if m:
            ext = m.group(1).lower()
            if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
                return "jpg" if ext == "jpeg" else ext
    return default


def detect_image_format(data: bytes) -> str | None:
    """Detect image format from file header bytes. Returns extension or None."""
    if len(data) < 4:
        return None
    if data[0] == 0xFF and data[1] == 0xD8:
        return "jpg"
    if data[:3] == b"\x89PN":
        return "png"
    if data[:3] == b"GIF":
        return "gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return "heic"
    return None


def extract_hevc_from_wxgf(data: bytes) -> bytes | None:
    """Strip the wxgf private header and return the Annex-B HEVC stream.

    The wxgf container embeds an ICC profile before the real video stream,
    which can contain fake `00 00 00 01` start codes. Only trust a start code
    whose NAL type is VPS(32)/SPS(33)/PPS(34); the real stream begins there.
    """
    i = 0
    n = len(data)
    while i < n - 4:
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            nal_type = (data[i + 4] >> 1) & 0x3F
            if nal_type in (32, 33, 34):
                return data[i:]
        i += 1
    return None


def decode_wxgf_to_jpeg(data: bytes) -> bytes | None:
    """Decode a WeChat wxgf (HEVC) image to JPEG via ffmpeg."""
    hevc = extract_hevc_from_wxgf(data)
    if not hevc:
        return None
    tmp_in = tempfile.mktemp(suffix=".h265")
    tmp_out = tempfile.mktemp(suffix=".jpg")
    try:
        with open(tmp_in, "wb") as f:
            f.write(hevc)
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", tmp_in, "-frames:v", "1", "-y", tmp_out],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            return None
        with open(tmp_out, "rb") as f:
            return f.read() or None
    except Exception:
        return None
    finally:
        for p in (tmp_in, tmp_out):
            try:
                os.unlink(p)
            except OSError:
                pass


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.images_dir = os.path.join(data_dir, "images")
        os.makedirs(self.images_dir, exist_ok=True)
        self.log_path = os.path.join(data_dir, "messages.jsonl")
        self.devices_path = os.path.join(data_dir, "devices.json")
        self.lock = threading.Lock()
        self.messages: list[dict] = []
        self._recent: dict[tuple, float] = {}  # 去重窗口：key -> 时间戳
        self._device_names: dict[str, str] = {}  # deviceId -> 显示名（握手登记）
        self._load_devices()
        self._load()

    # ---------- 设备标识 ----------

    def _load_devices(self):
        try:
            with open(self.devices_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if isinstance(data, dict):
            self._device_names = {str(k): str(v) for k, v in data.items()}

    def _save_devices_locked(self):
        try:
            tmp = self.devices_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._device_names, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.devices_path)
        except OSError:
            pass

    def remember_device(self, device_id: str, device_name: str = "") -> None:
        """记住设备显示名（socket 握手时调用），落盘后重启仍可显示。

        名字为空时不清掉已有名字：HTTP 兜底上报只有 id、没有名字。
        """
        device_id = str(device_id or "").strip()
        device_name = str(device_name or "").strip()
        if not device_id:
            return
        with self.lock:
            if self._device_names.get(device_id) == device_name:
                return
            if not device_name and self._device_names.get(device_id):
                return
            self._device_names[device_id] = device_name
            self._save_devices_locked()

    def devices(self) -> list[dict]:
        """按设备聚合：消息数、覆盖群数、最近时间；名称来自握手登记。"""
        with self.lock:
            seen: dict[str, dict] = {}
            for r in self.messages:
                msg = r["msg"]
                did = device_id_of(msg)
                entry = seen.setdefault(
                    did, {"deviceId": did, "count": 0, "lastAt": 0, "rooms": set()})
                entry["count"] += 1
                entry["rooms"].add(msg.get("roomId") or "unknown")
                t = msg.get("createTime") or r["receivedAt"]
                if t > entry["lastAt"]:
                    entry["lastAt"] = t
            names = dict(self._device_names)
            out = []
            for did, entry in seen.items():
                out.append({
                    "deviceId": did,
                    "deviceName": names.get(did, ""),
                    "count": entry["count"],
                    "rooms": len(entry["rooms"]),
                    "lastAt": entry["lastAt"],
                })
            # 只有握手、还没上报过消息的设备也列出来（否则刚连上时网页看不到）
            for did, name in names.items():
                if did not in seen:
                    out.append({"deviceId": did, "deviceName": name,
                                "count": 0, "rooms": 0, "lastAt": 0})
            return sorted(out, key=lambda e: e["lastAt"], reverse=True)

    def _load(self):
        if os.path.exists(self.log_path):
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict) and "seq" in rec:
                        self.messages.append(rec)
            return
        imported = self._import_legacy()
        if not imported:
            return
        self._flush_locked()

    def _import_legacy(self) -> int:
        """首次启动时，把旧转发日志里的 POST 消息迁移进来。"""
        if not os.path.exists(LEGACY_LOG):
            return 0
        count = 0
        try:
            with open(LEGACY_LOG, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"method": "POST"' not in line:
                        continue
                    try:
                        raw = json.loads(line)
                        body = json.loads(raw.get("body") or "{}")
                    except Exception:
                        continue
                    if not isinstance(body, dict) or not body.get("roomId"):
                        continue
                    rec = self._build_record(body)
                    self.messages.append(rec)
                    count += 1
        except OSError:
            return 0
        return count

    def _build_record(self, msg: dict) -> dict:
        rec = {
            "seq": len(self.messages) + 1,
            "receivedAt": now_ms(),
            "msg": msg,
            "imgFile": None,
        }
        b64 = msg.get("imageBase64")
        if b64:
            self._save_image(rec, b64, msg.get("imagePath"), msg.get("imageKind"))
        return rec

    def _save_image(self, rec: dict, b64: str, img_path: str | None, kind: str | None):
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception:
            return
        if not data or len(data) > MAX_IMAGE_BYTES:
            return
        # 微信对不勾选“原图”的图片下发 wxgf 私有容器，转码成 JPEG 才能展示
        if detect_image_format(data) is None and data[:4] == b"wxgf":
            jpg = decode_wxgf_to_jpeg(data)
            if jpg is None:
                return
            data = jpg
            ext = "jpg"
        else:
            ext = guess_ext(img_path)
        msg = rec["msg"]
        name = safe_name(str(msg.get("msgSvrId") or "unknown"))
        kind_name = safe_name(str(kind or "img")) or "img"
        # 同一条消息可能由多台设备各存一份，文件名带设备前缀避免互相覆盖
        dev = safe_name(device_id_of(msg))
        prefix = f"{dev}_" if dev else ""
        fname = f"{prefix}{name}_{kind_name}.{ext}"
        full = os.path.join(self.images_dir, fname)
        try:
            with open(full, "wb") as f:
                f.write(data)
        except OSError:
            return
        rec["imgFile"] = f"images/{fname}"
        rec["imgSize"] = len(data)

    @staticmethod
    def _dedupe_key(msg: dict):
        """同一消息 60s 内的重复投递（socket 重传 / HTTP+socket 双通道）只收一次。

        key 里带 deviceId：去重只在同一台设备内生效，两台手机各报到一次会各存一份。
        含 imageSize 与是否带图：缩略图/中图/原图是不同记录，官方原图回填
        与 CDN 原图大小不同也不会互相吞掉。
        """
        svr = msg.get("msgSvrId")
        if not svr:
            return None
        return (
            device_id_of(msg),
            str(svr),
            str(msg.get("imageKind") or ""),
            str(msg.get("imageSize") or ""),
            bool(msg.get("imageBase64") or msg.get("cdnUrl")),
        )

    def add_message(self, msg: dict) -> dict | None:
        with self.lock:
            key = self._dedupe_key(msg)
            if key is not None:
                now = time.time()
                if len(self._recent) > 500:
                    self._recent = {k: t for k, t in self._recent.items() if now - t < 60}
                if key in self._recent and now - self._recent[key] < 60:
                    return None
                self._recent[key] = now
            rec = self._build_record(msg)
            self.messages.append(rec)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._maybe_fetch_cdn(rec)
            return rec

    def _maybe_fetch_cdn(self, rec: dict):
        """后台尝试拉取 cdnUrl 图片（手机可达的 CDN 链接服务器通常也可达）。"""
        msg = rec["msg"]
        url = msg.get("cdnUrl")
        if rec.get("imgFile") or not url or not str(url).startswith(("http://", "https://")):
            return
        threading.Thread(target=self._fetch_cdn, args=(rec, url), daemon=True).start()

    def _fetch_cdn(self, rec: dict, url: str):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read(MAX_IMAGE_BYTES + 1)
        except Exception:
            return
        if len(data) > MAX_IMAGE_BYTES or not data:
            return
        msg = rec["msg"]
        name = safe_name(str(msg.get("msgSvrId") or "unknown"))
        dev = safe_name(device_id_of(msg))
        prefix = f"{dev}_" if dev else ""
        fname = f"{prefix}{name}_original.jpg"
        full = os.path.join(self.images_dir, fname)
        try:
            with open(full, "wb") as f:
                f.write(data)
        except OSError:
            return
        with self.lock:
            rec["imgFile"] = f"images/{fname}"
            rec["imgSize"] = len(data)
            # 更新落盘记录
            self._rewrite_locked()

    def _rewrite_locked(self):
        with open(self.log_path, "w", encoding="utf-8") as f:
            for r in self.messages:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _flush_locked(self):
        with open(self.log_path, "w", encoding="utf-8") as f:
            for r in self.messages:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def list_messages(self, after: int = 0, limit: int = 1000, device: str | None = None):
        with self.lock:
            rows = self.messages
            if device is not None:
                rows = [r for r in rows if device_id_of(r.get("msg")) == device]
            total = len(rows)
            start = max(after, 0)
            end = min(start + limit, total)
            return rows[start:end], total, start

    def query_messages(self, room: str | None = None, device: str | None = None, keyword: str = "",
                       sender: str = "", since_ms: int = 0, before_ms: int = 0, limit: int = 50,
                       exclude_svr=None) -> list[dict]:
        """从新到旧扫描消息记录（Agent 工具 / 群 @ 上下文用），最多 limit 条。

        keyword 匹配正文；sender 匹配发送人 ID 或显示名；时间用 createTime（缺失时用 receivedAt）。
        device 的约定同 list_messages：None 不筛选，"" 只看无设备标识的旧数据。
        """
        needle = str(keyword or "").strip().lower()
        who = str(sender or "").strip().lower()
        out = []
        with self.lock:
            for r in reversed(self.messages):
                msg = r.get("msg") or {}
                if device is not None and device_id_of(msg) != device:
                    continue
                if room and msg.get("roomId") != room:
                    continue
                at = msg.get("createTime") or r.get("receivedAt") or 0
                if (before_ms and at > before_ms) or (since_ms and at < since_ms):
                    continue
                if exclude_svr not in (None, "") and str(msg.get("msgSvrId")) == str(exclude_svr):
                    continue
                if who and who not in (str(msg.get("sender") or "") + " " + str(msg.get("senderName") or "")).lower():
                    continue
                if needle and needle not in str(msg.get("content") or "").lower():
                    continue
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def rooms(self, device: str | None = None):
        with self.lock:
            seen: dict[str, dict] = {}
            for r in self.messages:
                msg = r["msg"]
                if device is not None and device_id_of(msg) != device:
                    continue
                rid = msg.get("roomId") or "unknown"
                entry = seen.setdefault(rid, {"roomId": rid, "count": 0, "lastAt": 0, "roomName": None})
                entry["count"] += 1
                # 群名称可能只在部分记录里解析成功（旧数据/解析失败），取最新的非空值
                if msg.get("roomName"):
                    entry["roomName"] = msg["roomName"]
                t = msg.get("createTime") or r["receivedAt"]
                if t > entry["lastAt"]:
                    entry["lastAt"] = t
            return sorted(seen.values(), key=lambda e: e["lastAt"], reverse=True)

    def stats(self, device: str | None = None):
        with self.lock:
            rows = self.messages
            if device is not None:
                rows = [r for r in rows if device_id_of(r.get("msg")) == device]
            images = sum(1 for r in rows if r.get("imgFile"))
            return {"count": len(rows), "images": images}

    def clear(self, room_id: str | None = None, device: str | None = None):
        """清空消息；room_id / device 可单独或组合限定范围，缺省清全部。"""
        with self.lock:
            removed = []
            if room_id or device is not None:
                kept = []
                for r in self.messages:
                    msg = r["msg"]
                    if room_id and msg.get("roomId") != room_id:
                        kept.append(r)
                    elif device is not None and device_id_of(msg) != device:
                        kept.append(r)
                    else:
                        removed.append(r)
                self.messages = kept
            else:
                removed = self.messages
                self.messages = []
            for r in removed:
                img = r.get("imgFile")
                if img:
                    try:
                        os.remove(os.path.join(self.data_dir, img))
                    except OSError:
                        pass
            self._rewrite_locked()
            return len(self.messages)

    def image_path(self, rel: str):
        # 防目录穿越
        full = os.path.realpath(os.path.join(self.data_dir, rel))
        root = os.path.realpath(self.data_dir)
        if not full.startswith(root + os.sep):
            return None
        return full if os.path.isfile(full) else None


class QuoteLog:
    """增量读取 agent 写入的 quotes.jsonl（agent 是唯一写入方，append-only）。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.rows: list[dict] = []
        self.offset = 0
        self._file_identity = None

    def _reload_locked(self):
        try:
            stat = os.stat(self.path)
            size = stat.st_size
        except OSError:
            return
        identity = (stat.st_dev, stat.st_ino)
        if identity != self._file_identity or size < self.offset:  # 文件被重建（backfill）
            self.rows = []
            self.offset = 0
            self._file_identity = identity
        if size == self.offset:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return
        self.offset += last_nl + 1  # 半行留到下一轮
        for line in data[:last_nl + 1].decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and "qseq" in row:
                self.rows.append(row)

    def list(self, after: int = 0, limit: int = 500, room: str | None = None,
             device: str | None = None):
        with self.lock:
            self._reload_locked()
            rows = self.rows
            if room:
                rows = [r for r in rows if r.get("roomId") == room]
            if device is not None:
                rows = [r for r in rows if str(r.get("deviceId") or "") == device]
            total = rows[-1]["qseq"] if rows else 0
            fresh = [r for r in rows if r["qseq"] > after][:limit]
            return fresh, total
