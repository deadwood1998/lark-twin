"""larkx work-memory: 看工作记忆状态、看某段时间的增量、手动跑归档规则。"""
import json
import time
from datetime import datetime

from .... import state
from ... import require
from . import feed, layout, sessions


def register(sub):
    p = sub.add_parser('work-memory', help='[记忆策略 work] 工作记忆: status 看状态 / feed 看某段时间的增量(只含没开免打扰的会话) / '
                                      'archive 手动跑归档规则')
    p.add_argument('action', nargs='?', default='status', choices=['status', 'feed', 'archive'])
    p.add_argument('--since', default='1h', help='feed: 从多久前开始(如 1h、30m)或 unix 秒')
    p.add_argument('--until', default='', help='feed: 到什么时候, 默认现在')
    p.add_argument('--part', type=int, default=0, help='feed: 只看第几段')
    p.add_argument('--refresh', action='store_true', help='feed: 先现查一次这些会话的免打扰状态')
    p.add_argument('--sessions', action='store_true', help='feed: 看本人 Codex / Claude Code 会话这一段(输入和最终输出)')
    p.add_argument('--dry-run', action='store_true', help='archive: 只列出会做什么')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_memory)


def cmd_memory(args):
    from ....cli import _fail, _print_json, _since_ts
    from ....config import load_config
    from ....storage import Storage
    cfg = load_config()
    hint = require('memory', 'work', cfg)
    if hint:
        return _fail(args, hint)
    home = cfg['agent_home']
    st = Storage()
    now = time.time()
    if args.action == 'archive':
        result = layout.maintain(home, dry_run=args.dry_run)
        if args.json:
            _print_json(result)
        else:
            print(('[预演] ' if args.dry_run else '') + layout.summarize(result))
            for old, new, why in result['files']:
                print(f'  {old} → {new}  {why}')
            for f, n, rel in result['items']:
                print(f'  {f}: {n} 条近况 → {rel}')
        return 0
    if args.action == 'feed':
        since = _since_ts(args.since, now)
        until = _since_ts(args.until, now) if args.until else int(now)
        if args.refresh:
            from ....protocol import LarkAuth, LarkClient
            feed.refresh_reminds(st, LarkClient(LarkAuth()), feed.chats_with_messages(st, since, until))
        if args.sessions:
            parts = sessions.split(sessions.render(sessions.collect(since, until)[0]), feed.FEED_PART_CHARS)
        else:
            parts = feed.build_feed(st, since, until)
        if args.part:
            if not 1 <= args.part <= max(len(parts), 1):
                return _fail(args, f'只有 {len(parts)} 段')
            parts = parts[args.part - 1:args.part]
        print(f'# 增量 {layout.window_label(since, until)}, 共 {len(parts)} 段')
        for part in parts:
            print(part + '\n')
        return 0
    # status
    root = layout.work_dir(home)
    chats = feed.feed_chats(st, int(now - 3600), int(now))
    try:
        digest_until = json.loads((state.STATE_DIR / 'daemon.json').read_text(encoding='utf-8')).get('digest_at')
    except (FileNotFoundError, ValueError):
        digest_until = None
    info = {
        'dir': str(root),
        'overview_chars': layout.overview_size(home), 'overview_budget': layout.OVERVIEW_BUDGET,
        'counts': {d: len(list((root / d).glob('*.md'))) for d in layout.DIMS},
        'archive_weeks': sorted(p.name for p in (root / 'archive').glob('*-W*')) if (root / 'archive').exists() else [],
        'digest_interval': cfg.get('memory_digest_interval'),
        'digested_until': datetime.fromtimestamp(digest_until).strftime('%Y-%m-%d %H:%M') if digest_until else None,
        'last_hour_chats': {k: len(v) for k, v in chats.items()},
        'sessions': bool(cfg.get('memory_sessions')),
    }
    if args.json:
        _print_json(info)
        return 0
    c = info['counts']
    print(f"工作记忆 {info['dir']}")
    print(f"  总览 {info['overview_chars']}/{info['overview_budget']} 字; 项目 {c['projects']} 个, 任务 {c['tasks']} 个, 人 {c['people']} 个;"
          f" 归档 {len(info['archive_weeks'])} 周")
    every = info['digest_interval']
    print(f"  消化: {'每 %s 秒一次' % every if every else '已关闭'}, 已消化到 {info['digested_until'] or '(还没跑过)'}")
    h = info['last_hour_chats']
    print(f"  本人的 Codex / Claude Code 会话: {'一起消化(只取输入和最终输出)' if info['sessions'] else '不读'}")
    print(f"  最近 1 小时有消息的会话: 消化 {h['include']} 个, 免打扰跳过 {h['muted']} 个, 机器人推送跳过 {h['bot']} 个, 状态未知跳过 {h['unknown']} 个")
    return 0
