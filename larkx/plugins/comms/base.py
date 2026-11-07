"""交互协议的基类: 数字人和别人怎么往来(没人叫它时要不要接话、回复怎么写、谁能让它干多少活)。
每个钩子的默认实现就是上游 lark-twin 的行为, 子类只覆盖自己要改的。

调用点:
- 配置: configure(cfg)                                  load_config 的最后一步
- 人设 / skill: persona() / skill()                     拼进 CLAUDE.md 的插件区块、数字人的 larkx skill
- 收到消息: listen_profile(daemon, msg)                  没人叫数字人(不是本人、不在接管、没触发)时, 要不要旁听这条
            listen_ok(daemon, msg, profile, chat_name)  旁听的这条要不要真的交给数字人(限流、去重、过滤机器人)
- 处理消息: message_context(daemon, ev)                  排队之后、交给数字人之前: 跳过 / 开发模式 / 附加说明
- 提示词: prompt_rules(type_name, is_owner, dev)         回复格式、访客规则
- CLI: register_cli(sub)
"""


class CommsProtocol:
    name = ''
    description = ''
    config = {}
    models_module = ''

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @classmethod
    def configure(cls, cfg: dict):
        pass

    @classmethod
    def register_cli(cls, sub):
        pass

    def persona(self) -> str:
        return ''

    def skill(self) -> str:
        return ''

    def listen_profile(self, daemon, msg: dict):
        """返回非空(这个会话的配置)表示旁听这条: 不用 @、不用前缀也交给数字人。上游: 不旁听。"""
        return None

    async def listen_ok(self, daemon, msg: dict, profile, chat_name: str = '') -> bool:
        return True

    def message_context(self, daemon, ev) -> dict:
        """skip: 这条不处理了(如排队期间关了旁听); dev: 别人发起的这一轮不按访客收窄;
        note: 附在这一轮提示词里的说明; watch_note: 本人在线(larkx watch)时附的说明。"""
        return {'skip': False, 'dev': False, 'note': '', 'watch_note': ''}

    def prompt_rules(self, type_name: str, is_owner: bool, dev: bool) -> tuple:
        """(接在回复指引后面的规则, 替换默认访客规则的说明或 None)。上游: 不加, 用默认访客规则。"""
        return '', None

    def status(self) -> dict:
        return {}
