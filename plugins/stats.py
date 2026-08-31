"""统计插件：/stats 消息/图片/报价/机器人状态。"""


def register(api):
    def stats_cmd(api, event, args):
        lines = []
        if api.store is not None:
            s = api.store.stats()
            lines.append(f"消息 {s.get('count', 0)} 条 · 图片 {s.get('images', 0)} 张")
        if api.quotes is not None:
            rows, total = api.quotes.list(0, 1)
            lines.append(f"报价记录 {total} 条")
        if api.bot is not None:
            st = api.bot.bot_status()
            lines.append("机器人：push=" + st.get("push_mode") + " qna=" +
                         ("开" if st.get("qna_enabled") else "关") +
                         " agent=" + str(st.get("agent") or "无") +
                         " 已推送=" + str(st.get("pushed")))
        if not lines:
            return "暂无统计信息。"
        return "\n".join(lines)

    api.register_command("stats", stats_cmd, usage="/stats", description="查看捕获与机器人状态")