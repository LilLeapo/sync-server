#!/usr/bin/env python3
"""每台设备的 Agent 设置：自动回复、报价提取、自定义提示词、可用工具。

不同设备的 Agent 用途可以不同（不一定做报价）。没单独设置过的设备用默认值
——目前全部开启、工具全选，与升级前的行为一致。落盘 data/device_settings.json，
只存改过的设备；key 为 deviceId，"" 表示没有设备标识的旧模块。
"""
from __future__ import annotations

import json
import os
import sys
import threading

MAX_PROMPT_CHARS = 4000
# tools=None 表示「全部工具」：以后新增的工具对没改过设置的设备自动可用；
# 但不含 opt_in 工具（发送消息/图片等对外动作），它们必须显式勾选（tools 为列表）
DEFAULTS = {"reply": True, "quotes": True, "prompt": "", "tools": None}


def validate(patch: dict, tool_names: set[str] | None = None) -> dict:
    """校验要修改的字段；非法时抛 ValueError（不会部分写入）。"""
    if not isinstance(patch, dict):
        raise ValueError("settings must be an object")
    clean = {}
    for key, value in patch.items():
        if key in ("reply", "quotes"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            clean[key] = value
        elif key == "prompt":
            if not isinstance(value, str):
                raise ValueError("prompt must be a string")
            value = value.strip()
            if len(value) > MAX_PROMPT_CHARS:
                raise ValueError(f"prompt longer than {MAX_PROMPT_CHARS} chars")
            clean[key] = value
        elif key == "tools":
            if value is not None:
                if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
                    raise ValueError("tools must be a list of names or null")
                unknown = [t for t in value if tool_names is not None and t not in tool_names]
                if unknown:
                    raise ValueError("unknown tools: " + ", ".join(unknown))
                value = list(dict.fromkeys(value))
            clean[key] = value
        else:
            raise ValueError(f"unknown setting: {key}")
    return clean


class DeviceSettings:
    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._saved: dict[str, dict] = {}
        self._load()

    def get(self, device_id: str | None) -> dict:
        with self._lock:
            return self._merged(str(device_id or ""))

    def all(self) -> dict[str, dict]:
        """改过设置的设备（合并默认值后）。"""
        with self._lock:
            return {did: self._merged(did) for did in self._saved}

    def update(self, device_id: str | None, patch: dict, tool_names: set[str] | None = None) -> dict:
        clean = validate(patch, tool_names)
        did = str(device_id or "")
        with self._lock:
            self._saved[did] = {**self._saved.get(did, {}), **clean}
            self._save_locked()
            return self._merged(did)

    def _merged(self, device_id: str) -> dict:
        out = {**DEFAULTS, **self._saved.get(device_id, {})}
        if out["tools"] is not None:
            out["tools"] = list(out["tools"])
        return out

    def _load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[device_settings] 读取失败: {e}", file=sys.stderr)
            return
        if not isinstance(data, dict):
            return
        for did, patch in data.items():
            try:
                self._saved[str(did)] = validate(patch)
            except ValueError as e:
                print(f"[device_settings] 忽略设备 {did} 的非法设置: {e}", file=sys.stderr)

    def _save_locked(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._saved, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[device_settings] 保存失败: {e}", file=sys.stderr)
