"""钉钉机器人离线测试：签名、推送 payload、房间过滤、报价去重。"""
import base64
import hashlib
import hmac
import json

import pytest

from server.dingtalk import (
    DingTalkBot,
    DingTalkClient,
    webhook_sign,
)


def test_webhook_sign_algorithm():
    secret = "SECtest_secret_123"
    ts = 1589249512821
    got = webhook_sign(secret, ts)
    expect = base64.b64encode(
        hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
    ).decode()
    assert got == expect
    # 时间或密钥变化必须改变签名
    assert webhook_sign(secret, ts + 1) != got
    assert webhook_sign(secret + "x", ts) != got


def test_client_push_payload(monkeypatch):
    cfg = {"webhook": {"access_token": "tok123", "secret": "SECabc"}}
    client = DingTalkClient(cfg)
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"errcode": 0, "errmsg": "ok"}).encode()

    def fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = client.push_text("测试信息")
    assert out["errcode"] == 0
    assert captured["url"] == "https://oapi.dingtalk.com/robot/send?access_token=tok123"
    body = captured["body"]
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "测试信息"
    assert body["timestamp"] and body["sign"]
    assert webhook_sign("SECabc", int(body["timestamp"])) == body["sign"]


def test_bot_room_filter_and_dedupe(tmp_path):
    cfg = {
        "enabled": True,
        "mode": "webhook",
        "webhook": {"access_token": "tok", "secret": ""},
        "push_rooms": ["group_a"],
    }
    bot = DingTalkBot(cfg, data_dir=str(tmp_path))
    rows_a = [{"roomId": "group_a@chatroom", "msgSeq": 1, "供应商": "甲",
               "项目": "无氧杆", "价格": "100"}]
    rows_b = [{"roomId": "group_b@chatroom", "msgSeq": 1, "供应商": "乙",
               "项目": "保阻杆", "价格": "200"}]
    bot.notify_quote(rows_b)  # 不在 push_rooms 内
    assert bot._push_q.empty()
    bot.notify_quote(rows_a)
    assert bot._push_q.qsize() == 1
    bot.notify_quote(rows_a)  # 去重
    assert bot._push_q.qsize() == 1


def test_bot_requires_config():
    assert not DingTalkBot({"enabled": False},
                           data_dir="/tmp").active_push
    bot = DingTalkBot({"enabled": True, "mode": "webhook",
                       "webhook": {"access_token": ""}},
                      data_dir="/tmp")
    assert not bot.active_push
