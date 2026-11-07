"""插件切换: 默认就是上游原样; --memory work 一个开关同时切到配套的交互协议; 切回去时人设、skill 里的插件内容跟着去掉。"""
import argparse
import asyncio
import sys
import time

import pytest

from larkx import plugins
from larkx.agent.home import TEMPLATES, ensure_agent_home
from larkx.plugins.comms.default import DefaultComms
from larkx.plugins.comms.work import WorkComms
from larkx.config import load_config
from larkx.plugins.memory.default import DefaultMemory
from larkx.plugins.memory.work import WorkMemory

from test_reliability import _daemon


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ('LARKX_MEMORY', 'LARKX_COMMS', 'LARKX_AGENT_SESSION', 'LARKX_MEMORY_DIGEST_INTERVAL'):
        monkeypatch.delenv(key, raising=False)


def test_default_is_upstream():
    cfg = load_config()
    assert (cfg['memory'], cfg['comms']) == ('default', 'default')
    assert cfg['agent_session'] == 'single'                     # codex 后端单一 thread
    assert 'memory_digest_interval' not in cfg                  # work 策略的配置项不出现
    memory, comms = plugins.load(cfg)
    assert isinstance(memory, DefaultMemory) and isinstance(comms, DefaultComms)


def test_memory_flag_switches_comms_too(monkeypatch):
    monkeypatch.setenv('LARKX_MEMORY', 'work')
    cfg = load_config()
    assert (cfg['memory'], cfg['comms']) == ('work', 'work')
    assert cfg['agent_session'] == 'chat' and cfg['memory_digest_interval'] == 3600 and cfg['memory_sessions'] is True
    memory, comms = plugins.load(cfg)
    assert isinstance(memory, WorkMemory) and isinstance(comms, WorkComms)
    # 单独指定交互协议时以它为准; 用户自己写的会话策略也不被覆盖
    monkeypatch.setenv('LARKX_COMMS', 'default')
    monkeypatch.setenv('LARKX_AGENT_SESSION', 'single')
    monkeypatch.setenv('LARKX_MEMORY_DIGEST_INTERVAL', '0')
    cfg = load_config()
    assert (cfg['memory'], cfg['comms'], cfg['agent_session'], cfg['memory_digest_interval']) == ('work', 'default', 'single', 0)


def test_unknown_plugin_rejected(monkeypatch):
    monkeypatch.setenv('LARKX_MEMORY', 'nope')
    with pytest.raises(ValueError, match='可选'):
        load_config()


def test_cli_flag_sets_env_for_this_run(monkeypatch):
    from larkx import cli
    monkeypatch.setattr(sys, 'argv', ['larkx', '--memory', 'work', 'daemon'])
    cli._plugin_flags()
    assert sys.argv == ['larkx', 'daemon'] and load_config()['comms'] == 'work'
    # larkx plugin use --memory X 是它自己的参数, 不被当成全局开关吃掉
    monkeypatch.setattr(sys, 'argv', ['larkx', 'plugin', 'use', '--memory', 'default'])
    cli._plugin_flags()
    assert sys.argv == ['larkx', 'plugin', 'use', '--memory', 'default']


def test_plugin_commands_need_plugin_enabled(capsys):
    from larkx.plugins.comms.work.cli import cmd_group
    from larkx.plugins.memory.work.cli import cmd_memory
    assert cmd_memory(argparse.Namespace(action='status', json=False)) == 1
    assert cmd_group(argparse.Namespace(action='list', json=False)) == 1
    assert 'larkx --memory work' in capsys.readouterr().err


def test_plugin_use_writes_env(tmp_path, monkeypatch, capsys):
    from larkx import cli
    env = tmp_path / '.env'
    env.write_text('LARKX_AGENT_BACKEND=codex\n# LARKX_MEMORY=work\nLARKX_COMMS=default\n', encoding='utf-8')
    monkeypatch.setattr(cli, '_env_file', lambda: env)
    ns = argparse.Namespace(action='use', use_memory='work', use_comms=None, json=False)
    assert cli.cmd_plugin(ns) == 0
    text = env.read_text(encoding='utf-8')
    # 只换记忆策略: 交互协议回到"跟着记忆策略走", 其他行原样
    assert 'LARKX_MEMORY=work' in text and 'LARKX_COMMS' not in text.replace('# LARKX_MEMORY=work', '')
    assert 'LARKX_AGENT_BACKEND=codex' in text and '# LARKX_MEMORY=work' in text
    assert '记忆策略 work, 交互协议 work' in capsys.readouterr().out
    assert cli.cmd_plugin(argparse.Namespace(action='use', use_memory=None, use_comms='nope', json=False)) == 1


def test_persona_and_skill_follow_plugins(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.update(agent_home=str(tmp_path / 'agent'), agent_claude_dir=str(tmp_path / 'claude'))
    home = ensure_agent_home(cfg)
    template = (TEMPLATES / 'CLAUDE.md').read_text(encoding='utf-8')
    skill = home / '.claude' / 'skills' / 'larkx' / 'SKILL.md'
    assert (home / 'CLAUDE.md').read_text(encoding='utf-8') == template          # 默认: 和上游模板一字不差
    assert '工作记忆' not in skill.read_text(encoding='utf-8')
    assert not (home / 'memory' / 'work').exists()

    monkeypatch.setenv('LARKX_MEMORY', 'work')
    cfg = dict(load_config(), agent_home=cfg['agent_home'], agent_claude_dir=cfg['agent_claude_dir'])
    ensure_agent_home(cfg)
    text = (home / 'CLAUDE.md').read_text(encoding='utf-8')
    assert text.startswith(template.rstrip('\n')) and plugins.PERSONA_BEGIN in text
    assert 'memory/work/' in text and '「张三：」' in text                       # 记忆策略 + 交互协议各自的说明
    assert (home / 'memory' / 'work' / 'RULES.md').exists()
    assert 'larkx work-memory' in skill.read_text(encoding='utf-8') and 'larkx group' in skill.read_text(encoding='utf-8')
    ensure_agent_home(cfg)                                                    # 再启动一次: 区块不重复
    assert (home / 'CLAUDE.md').read_text(encoding='utf-8').count(plugins.PERSONA_BEGIN) == 1

    # 用户在区块外改过人设: 切回默认时只去掉插件区块, 用户的改动保留
    custom = (home / 'CLAUDE.md').read_text(encoding='utf-8').replace('# ', '# 我改过的 ', 1)
    (home / 'CLAUDE.md').write_text(custom, encoding='utf-8')
    monkeypatch.delenv('LARKX_MEMORY')
    cfg = dict(load_config(), agent_home=cfg['agent_home'], agent_claude_dir=cfg['agent_claude_dir'])
    ensure_agent_home(cfg)
    text = (home / 'CLAUDE.md').read_text(encoding='utf-8')
    assert plugins.PERSONA_BEGIN not in text and '我改过的' in text
    assert 'larkx group set' not in skill.read_text(encoding='utf-8') and 'larkx group list' not in skill.read_text(encoding='utf-8')


def test_default_comms_does_not_listen(tmp_path, monkeypatch, fake_client):
    """上游行为: 唤起白名单里的人私聊不带 /ai 不处理; 有会话配置也不旁听。"""
    from larkx.plugins.comms.work import profiles
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.storage.whitelist_add('201', '李四')
    profiles.save(obj.storage, 'g1', listen=1)
    msgs = [{'msg_id': 'p1', 'chat_id': 'p-li', 'chat_type': 1, 'from_id': '201', 'content': '在吗', 'create_time': int(time.time())},
            {'msg_id': 'g1m', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '202', 'content': '接口挂了', 'create_time': int(time.time())}]

    async def go():
        for m in msgs:
            await obj.on_message(m, '', '李四')
    asyncio.run(go())
    assert obj.inbox.qsize() == 0
    # 换成 work 协议: 两条都旁听
    obj.comms = WorkComms(obj.cfg)
    msgs = [dict(m, msg_id=m['msg_id'] + 'x') for m in msgs]
    asyncio.run(go())
    assert obj.inbox.qsize() == 2


def test_default_memory_asks_like_upstream(tmp_path, monkeypatch, fake_client):
    from larkx.daemon import Event
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    calls = []

    async def ask(prompt, **kw):
        calls.append(kw)
    obj.session.ask = ask
    asyncio.run(obj._run_turn(Event('message', 'x', chat_id='g1'), 'x'))
    assert calls == [{}] and obj.memory.replaced_jobs() == set()
