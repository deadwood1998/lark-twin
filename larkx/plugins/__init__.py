"""插件: 在上游 lark-twin 的基础上, 一键切换的记忆策略(larkx/plugins/memory/)和交互协议(larkx/plugins/comms/)。

两个维度, 各自一个注册表, 用名字选:
- 记忆策略 LARKX_MEMORY(或 --memory): default = 上游原样(memory/MEMORY.md + 每日回顾, codex 单一 thread);
  其他策略在 larkx/plugins/memory/<名>/ 下。
- 交互协议 LARKX_COMMS(或 --comms): default = 上游原样; 其他协议在 larkx/plugins/comms/<名>/ 下。
  不写时跟着记忆策略走(策略的 default_comms), 所以 `--memory work` 一个开关就同时切到配套的协议。

插件能做的事都是基类里的钩子(larkx/plugins/memory/base.py、larkx/plugins/comms/base.py), 默认实现就是上游的行为;
daemon、CLI、数字人目录只在这些钩子处调用插件, 不关心具体是哪个。新增一个策略或协议:
在对应目录下建一个子包, 实现基类, 再把它登记进对应 __init__.py 的 REGISTRY。见 docs/plugins.md。

和插件无关、默认就有的能力(富文本、云文档评论、发文件、larkx watch / inbox / ping 等)不在这里, 一直开着。
"""
import importlib
import os

DEFAULT = 'default'
KINDS = ('memory', 'comms')
ENV = {'memory': 'LARKX_MEMORY', 'comms': 'LARKX_COMMS'}
# 插件在数字人人设(CLAUDE.md)末尾维护的区块: 每次启动按启用的插件重写, 用户在区块外的改动不受影响
PERSONA_BEGIN = '<!-- larkx:plugins 这一段由 larkx 按启用的插件生成, 每次启动覆盖, 不要改 -->'
PERSONA_END = '<!-- /larkx:plugins -->'


def registry(kind: str) -> dict:
    """{名字: 'module:Class'}"""
    if kind == 'memory':
        from .memory import REGISTRY
    elif kind == 'comms':
        from .comms import REGISTRY
    else:
        raise ValueError(f'未知的插件种类 {kind}')
    return REGISTRY


def names(kind: str) -> list:
    return list(registry(kind))


def plugin_class(kind: str, name: str):
    reg = registry(kind)
    if name not in reg:
        raise ValueError(f'未知的{"记忆策略" if kind == "memory" else "交互协议"} {name!r}, 可选: {", ".join(reg)}')
    module, _, cls = reg[name].partition(':')
    return getattr(importlib.import_module(module), cls)


def selected(cfg: dict) -> dict:
    """{'memory': 名字, 'comms': 名字}: comms 没写时跟着记忆策略配套的协议走。"""
    memory = (cfg.get('memory') or DEFAULT).strip()
    comms = (cfg.get('comms') or '').strip()
    if not comms:
        comms = getattr(plugin_class('memory', memory), 'default_comms', DEFAULT) or DEFAULT
    return {'memory': memory, 'comms': comms}


def _cast(default, value: str):
    if isinstance(default, bool):
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def apply_config(cfg: dict) -> dict:
    """load_config 的最后一步: 定下启用的插件, 补上它们自己的配置项(LARKX_<KEY>), 再让它们调整基础配置。"""
    chosen = selected(cfg)
    cfg.update(chosen)
    for kind in KINDS:
        cls = plugin_class(kind, chosen[kind])
        for key, default in (getattr(cls, 'config', None) or {}).items():
            v = os.environ.get('LARKX_' + key.upper())
            cfg[key] = _cast(default, v) if v not in (None, '') else cfg.get(key, default)
        cls.configure(cfg)
    return cfg


def load(cfg: dict) -> tuple:
    """按配置实例化 (记忆策略, 交互协议)。"""
    chosen = selected(cfg)
    return (plugin_class('memory', chosen['memory'])(cfg), plugin_class('comms', chosen['comms'])(cfg))


def load_models():
    """所有已登记插件的数据表(不管启没启用): 建表、补列都在 Storage 初始化时一次做完, 切换插件不用迁移。"""
    for kind in KINDS:
        for name in names(kind):
            cls = plugin_class(kind, name)
            mod = getattr(cls, 'models_module', '')
            if mod:
                importlib.import_module(mod)


def register_cli(sub):
    """所有已登记插件的子命令。没启用时调用会提示怎么启用(用 --memory / --comms 可以临时指定)。"""
    for kind in KINDS:
        for name in names(kind):
            plugin_class(kind, name).register_cli(sub)


def require(kind: str, name: str, cfg: dict = None) -> str:
    """插件子命令开头检查: 插件没启用时返回提示, 启用了返回空。"""
    from ..config import load_config
    cfg = cfg or load_config()
    if cfg.get(kind) == name:
        return ''
    flag = '--memory' if kind == 'memory' else '--comms'
    return (f'这个命令属于{"记忆策略" if kind == "memory" else "交互协议"} {name}, 当前用的是 {cfg.get(kind)}。'
            f'临时指定: larkx {flag} {name} ...; 长期切换: larkx plugin use {flag} {name}')


def persona_block(memory, comms) -> str:
    parts = [p for p in (memory.persona(), comms.persona()) if p and p.strip()]
    if not parts:
        return ''
    return PERSONA_BEGIN + '\n\n' + '\n\n'.join(p.strip() for p in parts) + '\n\n' + PERSONA_END + '\n'


def strip_persona_block(text: str) -> str:
    start = text.find(PERSONA_BEGIN)
    if start < 0:
        return text
    end = text.find(PERSONA_END, start)
    end = len(text) if end < 0 else end + len(PERSONA_END)
    return (text[:start].rstrip('\n') + '\n' + text[end:].lstrip('\n')).rstrip('\n') + '\n'


def with_persona_block(text: str, block: str) -> str:
    base = strip_persona_block(text)
    return base if not block else base.rstrip('\n') + '\n\n' + block


def skill_text(base: str, memory, comms) -> str:
    """数字人的 larkx skill: 上游那份, 末尾接上启用的插件各自的说明。"""
    extra = [p.strip() for p in (memory.skill(), comms.skill()) if p and p.strip()]
    return base.rstrip('\n') + ('\n\n' + '\n\n'.join(extra) if extra else '') + '\n'


def describe(cfg: dict) -> str:
    chosen = selected(cfg)
    return f'记忆策略 {chosen["memory"]}, 交互协议 {chosen["comms"]}'
