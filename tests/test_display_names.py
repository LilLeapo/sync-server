"""显示名（群名称 / 发送者名称）回退逻辑测试。

模块从 1.1.0.r70 起在 ChatMessage 里附带 roomName / senderName；
服务端所有展示位置都应优先用显示名，缺失时回退到 ID（保持旧数据可用）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.pi_bridge import fallback_format, room_label  # noqa: E402
from server.quote_agent import room_display, sender_display  # noqa: E402
from server.store import Store  # noqa: E402


class TestDisplayHelpers(unittest.TestCase):
    def test_sender_prefers_name(self):
        self.assertEqual(sender_display({"sender": "wxid_a", "senderName": "陈工"}), "陈工")

    def test_sender_falls_back_to_id(self):
        self.assertEqual(sender_display({"sender": "wxid_a"}), "wxid_a")
        self.assertEqual(sender_display({"sender": "wxid_a", "senderName": ""}), "wxid_a")
        self.assertEqual(sender_display({}), "")

    def test_room_prefers_name(self):
        self.assertEqual(
            room_display({"roomId": "1@chatroom", "roomName": "富利铜业报价群"}),
            "富利铜业报价群")

    def test_room_falls_back_to_id_without_suffix(self):
        self.assertEqual(room_display({"roomId": "49945523870@chatroom"}), "49945523870")
        self.assertEqual(room_display({}), "unknown")

    def test_room_label_uses_rows(self):
        rows = [{"roomId": "1@chatroom", "roomName": "报价群"}]
        self.assertEqual(room_label("1@chatroom", rows), "报价群")
        # 无名称时回退到去后缀 ID
        self.assertEqual(room_label("1@chatroom", [{"roomId": "1@chatroom"}]), "1")
        self.assertEqual(room_label("1@chatroom", []), "1")

    def test_fallback_format_uses_room_name(self):
        text = fallback_format("1@chatroom", [{"roomId": "1@chatroom", "roomName": "报价群",
                                               "项目": "无氧杆", "价格": "100"}])
        self.assertIn("报价群", text)
        self.assertNotIn("1@chatroom", text)


class TestStoreRoomsRoomName(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rooms_aggregates_room_name(self):
        # 第二条不带名称（模拟旧数据 / 解析失败），不应覆盖已有的非空名称
        self.store.add_message({"roomId": "1@chatroom", "roomName": "报价群",
                                "sender": "wxid_a", "content": "x", "type": 1,
                                "msgSvrId": 1})
        self.store.add_message({"roomId": "1@chatroom", "sender": "wxid_b",
                                "content": "y", "type": 1, "msgSvrId": 2})
        rooms = self.store.rooms()
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["roomName"], "报价群")
        self.assertEqual(rooms[0]["roomId"], "1@chatroom")
        self.assertEqual(rooms[0]["count"], 2)

    def test_rooms_room_name_null_when_absent(self):
        self.store.add_message({"roomId": "2@chatroom", "sender": "wxid_a",
                                "content": "x", "type": 1, "msgSvrId": 3})
        self.assertIsNone(self.store.rooms()[0]["roomName"])


if __name__ == "__main__":
    unittest.main()