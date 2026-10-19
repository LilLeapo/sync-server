"""回声示例插件 —— 最小插件模板，照它抄新插件即可。

三步写一个插件：
1. 在 plugins/ 下新建 xxx.py，定义 register(api)；
2. 用 api.register_command / register_message_hook / register_quote_hook 绑定逻辑，
   或用 api.register_agent_tool 给问答 Agent 加工具；
3. 重启 sync-server（开发期可用 /help 验证是否加载成功）。

运行期可用资源：api.store（消息库）/ api.quotes（报价库）/ api.bridge（Agent）/
api.feishu（飞书客户端）/ api.config（飞书配置）/ api.reply(text) / api.log(msg)。
"""


def register(api):
    def echo_cmd(api, event, args):
        if not args.strip():
            return "用法：/echo 随便说点什么"
        return args.strip()

    api.register_command("echo", echo_cmd, usage="/echo <文本>", description="原样复读（示例插件）")

    def quote_watch(api, ctx):
        # 示例：监听报价钩子，只打日志，不拦截默认推送（返回 None 即可）
        api.log(f"观察报价 {ctx['room']}，items={len(ctx['rows'])}")
        return None

    api.register_quote_hook(quote_watch)

    # 示例：给 Agent 加一个工具。参数用 tool_schema 声明（也可以直接传 JSON Schema）；
    # handler(params, ctx) 拿到的是校验、补默认值后的参数，ctx 含 store / quote_log / device / ledger。
    # opt_in=True：默认不启用，需在网页「设备设置」里勾选（飞书等机器人不提供 opt_in 工具）。
    from server.tool_schema import S

    def echo_tool(params, ctx):
        return params["text"] * params["times"]

    api.register_agent_tool(
        "echo_text", echo_tool, label="复读（示例）", description="把文本重复若干次（示例工具）",
        parameters=S.object({"text": S.string().required().description("要复读的文本"),
                             "times": S.integer().min(1).max(5).default(1).description("次数")}),
        opt_in=True)