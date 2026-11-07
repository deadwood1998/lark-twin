"""larkx group: 会话配置(群和私聊) —— 旁听、会话白名单、开发模式、专属指令。只有本人能改。"""
import os
import sys

from loguru import logger

from .... import state
from ... import require
from . import profiles
from .notes import profile_senders

ON_OFF = {'on': 1, 'off': 0}
P2P_ALIASES = ('p2p', '私聊', '所有私聊')


def register(sub):
    p = sub.add_parser('group', help='[交互协议 work] 会话配置(群和私聊): 旁听(不 @ 也交给数字人, 按需回复)、会话白名单、'
                                     '开发模式、专属指令; 数字人不能改')
    p.add_argument('action', choices=['list', 'show', 'set', 'remove'])
    p.add_argument('target', nargs='?', help='chat_id、群名或私聊对方的名字(本地会话里完全一致); p2p 表示所有私聊')
    p.add_argument('--listen', choices=['on', 'off'], help='旁听: 别人在这里发的每条消息都交给数字人, 它自己判断要不要回')
    p.add_argument('--dev', choices=['on', 'off'], help='开发模式: 别人发起的一轮也能查代码、跑命令、改代码')
    p.add_argument('--bots', choices=['on', 'off'], help='旁听时机器人发的也处理(默认 off)')
    p.add_argument('--instruction', help='本会话专属指令, 每一轮都附上; - 从 stdin 读; 空字符串清除')
    p.add_argument('--workdir', help='本会话相关的代码目录')
    p.add_argument('--max-turns', type=int, help='旁听每 10 分钟最多交给数字人几条(默认 20, 0 不限)')
    p.add_argument('--allow', action='append', metavar='名字或user_id',
                   help='加进本会话白名单, 可重复; 白名单非空时只旁听这些人、只对这些人开开发模式')
    p.add_argument('--disallow', action='append', metavar='名字或user_id', help='移出本会话白名单, 可重复')
    p.add_argument('--allow-all', action='store_true', help='清空本会话白名单, 恢复为所有人')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_group)


def _profile_dict(row) -> dict:
    return {'chat_id': row.chat_id, 'chat_name': row.chat_name, 'listen': bool(row.listen), 'dev': bool(row.dev),
            'bots': bool(row.bots), 'max_turns': row.max_turns, 'workdir': row.workdir, 'instruction': row.instruction,
            'senders': profile_senders(row), 'updated_at': row.updated_at}


def _profile_line(row) -> str:
    flags = [f'旁听{"开" if row.listen else "关"}', f'开发模式{"开" if row.dev else "关"}']
    if row.listen:
        flags.append(f'机器人{"也处理" if row.bots else "不处理"}')
        flags.append(f'10 分钟最多 {row.max_turns} 条' if row.max_turns else '不限流')
    senders = profile_senders(row)
    flags.append('白名单: ' + '、'.join(f'{n or "?"}({u})' for u, n in senders.items()) if senders else '所有人')
    out = f'{row.chat_id}\t{row.chat_name or "-"}\t{", ".join(flags)}'
    if row.workdir:
        out += f'\n    代码目录: {row.workdir}'
    if row.instruction:
        out += '\n    专属指令: ' + row.instruction.strip().replace('\n', '\n              ')
    return out


def _resolve_chat(st, target: str):
    """chat_id 或本地会话里的群名(要完全一致); 返回 (chat_id, 名字) 或 (None, 错误)。"""
    target = (target or '').strip()
    if target in P2P_ALIASES:
        return 'p2p', '所有私聊'
    if target.isdigit():
        return target, st.get_chat_name(target)
    hits = [c for c in st.list_chats(limit=5000) if (c.name or '') == target]
    if len(hits) == 1:
        return hits[0].chat_id, hits[0].name
    if not hits:
        return None, f'本地会话里没有叫「{target}」的, 用 larkx chats 查 chat_id'
    return None, f'有 {len(hits)} 个会话叫「{target}」, 用 chat_id 指定: ' + ', '.join(c.chat_id for c in hits)


def _resolve_person(st, query: str):
    """user_id 或名字 → ({user_id: 名字}, 错误)。名字先在本地消息里找, 找不到再在线搜; 同名时要求用 user_id。"""
    from .... import whitelist as wl
    from ....protocol import LarkAuth, LarkClient
    query = (query or '').strip()
    if wl.is_user_id(query):
        return {query: ''}, ''
    hits = profiles.sender_ids_named(st, query)
    if not hits:
        try:
            people = [p for p in wl.search_people(LarkClient(LarkAuth()), query) if p['name'] == query]
        except Exception as e:
            return None, f'搜「{query}」失败: {e}'
        hits = {p['user_id']: p['name'] for p in people}
    if len(hits) == 1:
        return hits, ''
    if not hits:
        return None, f'找不到叫「{query}」的人, 用 user_id 指定'
    return None, f'有 {len(hits)} 个人叫「{query}」, 用 user_id 指定: ' + ', '.join(hits)


def cmd_group(args):
    """会话配置: 旁听(不 @ 也交给数字人, 它自己判断要不要回)、开发模式、专属指令。只有本人能改。"""
    from ....cli import _audit, _fail, _print_json
    from ....config import inside_twin, load_config
    from ....storage import Storage
    hint = require('comms', 'work')
    if hint:
        return _fail(args, hint)
    st = Storage()
    if args.action == 'list':
        rows = profiles.list_all(st)
        if args.json:
            _print_json([_profile_dict(r) for r in rows])
        elif not rows:
            print('还没有配置任何会话。例: larkx group set <chat_id或群名> --listen on --instruction "..."')
        else:
            print('\n'.join(_profile_line(r) for r in rows))
        return 0
    if not args.target:
        return _fail(args, f'用法: larkx group {args.action} <chat_id 或群名>')
    chat_id, name = _resolve_chat(st, args.target)
    if not chat_id:
        return _fail(args, name)
    if args.action == 'show':
        row = profiles.get(st, chat_id)
        if not row:
            return _fail(args, f'「{name or chat_id}」没有配置')
        _print_json(_profile_dict(row)) if args.json else print(_profile_line(row))
        return 0
    if inside_twin():
        # 数字人的会话里不能改: 免得群里有人让它给自己开旁听或开发模式
        turn = state.read_turn()
        st.log_action(actor='agent', action='group', chat_id=chat_id, content=' '.join(sys.argv[1:])[:300], ok=0,
                      error=profiles.DENIED, turn_kind=(turn or {}).get('kind', ''), turn_ref=(turn or {}).get('ref', ''))
        try:
            from ....notify import Notifier
            from ....protocol import LarkAuth, LarkClient
            Notifier(load_config(), LarkClient(LarkAuth())).alert(
                f'数字人想修改「{name or chat_id}」的会话配置(旁听/开发模式), 已拦截。只能由你在终端(larkx group)里改',
                key=f'group:{chat_id}', cooldown=600)
        except Exception as e:
            logger.debug(f'会话配置拦截告警没发出去: {e!r}')
        return _fail(args, profiles.DENIED, code=3)
    if args.action == 'remove':
        if not profiles.remove(st, chat_id):
            return _fail(args, f'「{name or chat_id}」没有配置')
        _audit(st, 'group', chat_id, f'删除会话配置 {name or chat_id}')
        _print_json({'ok': True, 'chat_id': chat_id}) if args.json else print(f'已删除「{name or chat_id}」的配置, 恢复默认规则。立即生效。')
        return 0
    instruction = args.instruction
    if instruction == '-':
        instruction = sys.stdin.read()
    if instruction is not None and len(instruction) > 16000:
        return _fail(args, '专属指令太长(最多 16000 字)')
    workdir = args.workdir
    if workdir:
        workdir = os.path.realpath(os.path.expanduser(workdir))
        if not os.path.isdir(workdir):
            return _fail(args, f'代码目录不存在: {workdir}')
    if args.max_turns is not None and args.max_turns < 0:
        return _fail(args, '--max-turns 不能是负数(0 表示不限)')
    senders = None
    allow, disallow = getattr(args, 'allow', None) or [], getattr(args, 'disallow', None) or []
    if allow or disallow or getattr(args, 'allow_all', False):
        current = profiles.get(st, chat_id)
        senders = {} if getattr(args, 'allow_all', False) else (profile_senders(current) if current else {})
        for q in allow:
            person, err = _resolve_person(st, q)
            if err:
                return _fail(args, err)
            senders.update({u: n or senders.get(u, '') for u, n in person.items()})
        for q in disallow:
            if q in senders:
                senders.pop(q)
                continue
            named = [u for u, n in senders.items() if n == q]
            if not named:
                return _fail(args, f'白名单里没有「{q}」')
            for u in named:
                senders.pop(u)
    row = profiles.save(st, chat_id, chat_name=name or None,
                        listen=ON_OFF.get(args.listen), dev=ON_OFF.get(args.dev), bots=ON_OFF.get(args.bots),
                        instruction=instruction, workdir=workdir, max_turns=args.max_turns, senders=senders)
    _audit(st, 'group', chat_id, '会话配置 ' + ' '.join(sys.argv[2:])[:280])
    if args.json:
        _print_json({'ok': True, 'profile': _profile_dict(row)})
        return 0
    print(_profile_line(row))
    if row.dev:
        print('注意: 开发模式下, 这个会话里任何人发起的一轮都能让数字人查代码、跑命令、改代码(仍只能在本会话发消息)。'
              '只给信得过的群开; 关掉: larkx group set ' + chat_id + ' --dev off')
    print('立即生效, 不用重启。')
    return 0
