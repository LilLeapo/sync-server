"""群聊列表插件：/rooms 查看已接入的微信群及消息量。"""


def register(api):
    def rooms_cmd(api, event, args):
        if api.store is None:
            return "存储不可用。"
        rooms = api.store.rooms()
        if not rooms:
            return "还没有群聊数据。"
        limit = 15
        lines = [f"已接入 {len(rooms)} 个群，最近活跃的前 {min(limit, len(rooms))} 个："]
        for r in rooms[:limit]:
            name = str(r.get("roomId") or "").replace("@chatroom", "")
            lines.append(f"- {name} · {r.get('count', 0)} 条")
        return "\n".join(lines)

    api.register_command("rooms", rooms_cmd, usage="/rooms", description="查看已接入的微信群列表")