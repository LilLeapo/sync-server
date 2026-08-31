"""帮助插件：/help 列出全部已注册指令（含第三方插件）。"""


def register(api):
    def run(api, event, args):
        return api.host.help_text()

    def on_text(api, event, text):
        if text.strip() in ("帮助", "help", "？", "?"):
            return api.host.help_text()
        return None

    api.register_command("help", run, usage="/help", description="查看所有可用指令")
    api.register_message_hook(on_text)