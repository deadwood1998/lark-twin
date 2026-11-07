"""记忆策略的基类。每个钩子的默认实现就是上游 lark-twin 的行为, 子类只覆盖自己要改的。

调用点:
- 配置: configure(cfg)                         load_config 的最后一步(larkx/plugins/__init__.py)
- 数字人目录: ensure_home(home)                 agent/home.py, 每次 daemon 启动
- 人设 / skill: persona() / skill()            拼进 CLAUDE.md 的插件区块、数字人的 larkx skill
- 交给数字人: ask_options(ev)                   daemon._run_turn, 作为 session.ask 的参数
- 调度: tick(daemon)                          daemon._schedule, 每 15 秒
- 例行任务: replaced_jobs()                    daemon/maintenance.py: 这些例行任务(larkx/jobs.py)由策略自己接管, 不再按时跑
- 一轮前后: skip_event(ev) / after_event()      daemon._process
- CLI: register_cli(sub)                        larkx 的子命令(没启用时提示怎么启用)
"""


class MemoryStrategy:
    name = ''
    description = ''
    # 这个策略配套的交互协议: 没单独指定 LARKX_COMMS 时跟着它切
    default_comms = 'default'
    # 这个策略自己的配置项和默认值, 读 LARKX_<KEY 大写>
    config = {}
    # 这个策略的数据表所在模块(import 时登记进 Base.metadata), 没有就留空
    models_module = ''

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @classmethod
    def configure(cls, cfg: dict):
        """调整基础配置。上游: codex 后端所有消息进同一个 thread。"""
        if not cfg.get('agent_session'):
            cfg['agent_session'] = 'single'

    @classmethod
    def register_cli(cls, sub):
        pass

    def ensure_home(self, home):
        pass

    def persona(self) -> str:
        return ''

    def skill(self) -> str:
        return ''

    def ask_options(self, ev) -> dict:
        """这一轮交给 session.ask 的额外参数(codex 后端: key / note / sandbox_dir)。上游: 不带。"""
        return {}

    def replaced_jobs(self) -> set:
        """由这个策略接管的例行任务名(如每日回顾 'review')。上游: 一个都不接管。"""
        return set()

    async def tick(self, daemon):
        pass

    def skip_event(self, ev) -> bool:
        return False

    async def after_event(self, daemon, ev, result):
        pass

    def status(self) -> dict:
        """larkx plugin list 里显示的状态。"""
        return {}
