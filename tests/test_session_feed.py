import json
import time

import pytest

from larkx.plugins.memory.work import sessions as sf

NOW = time.time()


def iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(ts))


@pytest.fixture
def roots(tmp_path, monkeypatch):
    codex, claude = tmp_path / 'codex', tmp_path / 'claude'
    (codex / '2026' / '11' / '01').mkdir(parents=True)
    (claude / '-Users-me-proj').mkdir(parents=True)
    monkeypatch.setattr(sf, 'CODEX_SESSIONS', codex)
    monkeypatch.setattr(sf, 'CLAUDE_PROJECTS', claude)
    monkeypatch.setattr(sf, '_git_branch', lambda cwd: 'feat/x' if cwd else '')
    return codex, claude


def jl(*rows):
    return ''.join(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n' for r in rows)


def codex_rows(ts, text, final):
    return [
        {'timestamp': iso(ts), 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                                    'content': [{'type': 'input_text', 'text': '# AGENTS.md instructions'}]}},
        {'timestamp': iso(ts), 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                                    'content': [{'type': 'input_text', 'text': text}]}},
        {'timestamp': iso(ts + 1), 'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 5000}},
        {'timestamp': iso(ts + 2), 'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': final}},
    ]


def test_codex_input_and_final_only_incremental(roots):
    codex, _ = roots
    f = codex / '2026' / '11' / '01' / 'rollout-a.jsonl'
    meta = {'timestamp': iso(NOW - 9000), 'type': 'session_meta', 'payload': {'cwd': '/repo/a', 'thread_source': 'user'}}
    f.write_text(jl(meta, *codex_rows(NOW - 7200, '旧的需求', '旧结论'), *codex_rows(NOW - 600, '修一下登录超时', '已修复并提 MR')))
    sessions, offsets = sf.collect(NOW - 3600, NOW, {})
    assert len(sessions) == 1
    s = sessions[0]
    assert (s['tool'], s['cwd'], s['branch']) == ('Codex', '/repo/a', 'feat/x')
    assert [(k, t) for _, k, t in s['items']] == [('user', '修一下登录超时'), ('final', '已修复并提 MR')]
    # 下一次只读新追加的
    with open(f, 'a') as fh:
        fh.write(jl(*codex_rows(NOW + 10, '再加个单测', '单测已加')))
        fh.write('{"timestamp": "半行还在写')
    sessions, offsets2 = sf.collect(NOW - 3600, NOW + 60, offsets)
    assert [t for _, _, t in sessions[0]['items']] == ['再加个单测', '单测已加']
    assert offsets2[str(f)] < f.stat().st_size   # 没写完的半行下次再读
    assert sf.collect(NOW - 3600, NOW + 60, offsets2)[0] == []


def test_codex_skips_subagent_threads(roots):
    codex, _ = roots
    meta = {'timestamp': iso(NOW), 'type': 'session_meta', 'payload': {'cwd': '/repo', 'thread_source': 'subagent'}}
    (codex / '2026' / '11' / '01' / 'sub.jsonl').write_text(jl(meta, *codex_rows(NOW - 60, '子任务', '子结论')))
    assert sf.collect(NOW - 3600, NOW)[0] == []


def test_claude_user_and_last_text_per_turn(roots):
    _, claude = roots
    base = {'isSidechain': False, 'cwd': '/repo/b', 'gitBranch': 'fix/y'}
    rows = [
        {'type': 'ai-title', 'aiTitle': '日志内存放大'},
        {**base, 'type': 'user', 'timestamp': iso(NOW - 900), 'message': {'role': 'user', 'content': '查一下 OOM'}},
        {**base, 'type': 'assistant', 'timestamp': iso(NOW - 890), 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '先看日志'}]}},
        {**base, 'type': 'user', 'timestamp': iso(NOW - 880), 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'y' * 3000}]}},
        {**base, 'type': 'assistant', 'timestamp': iso(NOW - 870), 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '根因是日志队列'}]}},
        {**base, 'type': 'user', 'isMeta': True, 'timestamp': iso(NOW - 860), 'message': {'role': 'user', 'content': '<system-reminder>x'}},
        {**base, 'type': 'user', 'timestamp': iso(NOW - 850), 'message': {'role': 'user', 'content': '<command-name>/clear</command-name>'}},
        {**base, 'type': 'user', 'timestamp': iso(NOW - 800), 'message': {'role': 'user', 'content': '修了吧'}},
        {**base, 'isSidechain': True, 'type': 'assistant', 'timestamp': iso(NOW - 790), 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '子 agent'}]}},
        {**base, 'type': 'assistant', 'timestamp': iso(NOW - 700), 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '已修复'}]}},
    ]
    (claude / '-Users-me-proj' / 's1.jsonl').write_text(jl(*rows))
    (s,), _ = sf.collect(NOW - 3600, NOW)
    assert (s['tool'], s['title'], s['cwd'], s['branch']) == ('Claude Code', '日志内存放大', '/repo/b', 'fix/y')
    assert [(k, t) for _, k, t in s['items']] == [('user', '查一下 OOM'), ('final', '根因是日志队列'),
                                                  ('user', '修了吧'), ('final', '已修复')]
    text = sf.render([s])
    assert text.startswith('# 本人的 AI 编码会话') and '分支=fix/y' in text and '最终输出: 已修复' in text


def test_old_files_and_split(roots):
    codex, _ = roots
    f = codex / '2026' / '11' / '01' / 'old.jsonl'
    f.write_text(jl(*codex_rows(NOW - 90000, 'x', 'y')))
    import os
    os.utime(f, (NOW - 90000, NOW - 90000))
    assert sf.collect(NOW - 3600, NOW)[0] == []
    parts = sf.split('# 头\n\n' + '\n\n'.join(['## a\n' + 'z' * 50] * 5), 120)
    assert len(parts) > 1 and all(p.startswith('# 头') and len(p) <= 130 for p in parts)
