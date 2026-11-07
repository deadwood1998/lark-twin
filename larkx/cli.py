import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from loguru import logger
from . import plugins, state
from .config import inside_twin, is_agent_actor, load_config, with_signature
from .protocol import REGIONS, AuthExpired, LarkAuth, LarkClient, get_region
from .storage import Storage

# 数字人(LARKX_ACTOR=agent)不能用的子命令: 原始网关接口、登录凭证、再起一个监听进程、管理自己的服务、
# 以本人身份给自己下指令(say)、改本机 agent 的 skill
AGENT_FORBIDDEN = {'api', 'auth', 'listen', 'daemon', 'service', 'doctor', 'say', 'skill', 'web', 'prune', 'backfill',
                   'plugin'}


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _fail(args, msg: str, code: int=1) -> int:
    if getattr(args, 'json', False):
        _print_json({'ok': False, 'error': msg})
    else:
        print(msg, file=sys.stderr)
    return code


def cmd_auth(args):
    auth = LarkAuth()
    # 新登录的区域: --region > 已有凭证的区域 > LARKX_REGION
    region = get_region(args.region) if args.region else auth.region
    if args.action == 'status':
        _print_json(auth.status())
        return 0
    if args.action == 'import':
        if args.cookie:
            cookie_str = args.cookie
        elif args.file:
            cookie_str = open(args.file, encoding='utf-8').read()
        else:
            logger.info('请粘贴 cookie 串,回车结束:')
            cookie_str = sys.stdin.readline()
        try:
            msg = auth.import_cookie_string(cookie_str, region)
            print(f'导入成功({region.name}): {msg}')
            return 0
        except (ValueError, AuthExpired) as e:
            print(f'导入失败: {e}', file=sys.stderr)
            return 1
    if args.action == 'check':
        ok, msg = auth.validate(fetch_profile=True)
        if ok:
            auth.save()
        print(('OK ' if ok else 'EXPIRED ') + msg)
        return 0 if ok else 1
    if args.action == 'qr':
        try:
            import qrcode as _qrcode
        except ImportError:
            print('需要 qrcode 包: pip install qrcode', file=sys.stderr)
            return 1
        from .protocol.auth import QR_STATUS_NAMES, QrLogin
        from .config import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        png_path = DATA_DIR / 'login_qr.png'

        def on_status(status, step):
            print(f'状态: {QR_STATUS_NAMES.get(status, status)} ({step})', flush=True)

        for attempt in range(1, 7):
            try:
                ql = QrLogin(region)
            except AuthExpired as e:
                print(str(e), file=sys.stderr)
                return 1
            qr = _qrcode.QRCode(border=1)
            qr.add_data(ql.qr_content)
            qr.make_image().save(str(png_path))
            print(f'[{attempt}/6] 二维码已刷新(请用 {region.label} App 扫码): {png_path}', flush=True)
            if attempt == 1:
                try:
                    qr.print_ascii(invert=True)
                except Exception:
                    qr.print_tty()
                sys.stdout.flush()
            ok, data = ql.wait(timeout=45, on_status=on_status)
            if ok:
                try:
                    msg = ql.finish(auth)
                    print(f'登录成功,凭证已保存: {msg}')
                    return 0
                except AuthExpired as e:
                    print(str(e), file=sys.stderr)
                    return 1
            print('当前二维码已失效,自动刷新...', flush=True)
        print('多次刷新后仍未完成登录,请重试', file=sys.stderr)
        return 1


def _ask_login_choice():
    try:
        import questionary
        ans = questionary.select(
            '选择登录方式:',
            choices=[
                questionary.Choice('扫码登录(推荐)', value='qr'),
                questionary.Choice('粘贴 cookie 登录', value='cookie'),
                questionary.Choice('退出', value='quit'),
            ],
        ).ask()
        if ans:
            return ans
    except Exception:
        pass
    print('  [1] 扫码登录(推荐)')
    print('  [2] 粘贴 cookie 登录')
    print('  [q] 退出')
    c = input('选择: ').strip().lower()
    return {'1': 'qr', '2': 'cookie'}.get(c, 'quit')


def _ask_cookie():
    try:
        import questionary
        ans = questionary.text('粘贴完整 cookie 后回车:').ask()
        if ans is not None:
            return ans
    except Exception:
        pass
    return input('粘贴完整 cookie 后回车: ').strip()


def _interactive_login():
    auth = LarkAuth()
    ok, msg = auth.validate(fetch_profile=True)
    if ok:
        auth.save()
        return auth
    if not sys.stdin.isatty():
        raise AuthExpired(msg)
    print(f'凭证缺失或已失效(区域: {auth.region.name},切换区域用 larkx auth qr --region <feishu|lark>): {msg}', flush=True)
    while True:
        choice = _ask_login_choice()
        if choice == 'qr':
            rc = cmd_auth(argparse.Namespace(action='qr', cookie=None, file=None, region=None))
            if rc == 0:
                auth2 = LarkAuth()
                ok2, _ = auth2.validate()
                if ok2:
                    return auth2
        elif choice == 'cookie':
            cookie_str = _ask_cookie()
            if cookie_str is None:
                raise SystemExit(1)
            try:
                auth.import_cookie_string(cookie_str)
                return auth
            except Exception as e:
                print(f'导入失败: {e}')
        else:
            raise SystemExit(1)


def _read_send_text(args) -> str:
    # 省略内容或写 - 时从 stdin 读: 多行和含引号、$ 的内容不经过 shell 参数拼接
    if not args.text or args.text == ['-']:
        if sys.stdin.isatty():
            return ''
        return sys.stdin.read().rstrip('\n')
    return ' '.join(args.text)


# 对外发出消息的动作: 数字人的发送上限把它们合起来算, 发出去的消息都能撤回
SENT_ACTIONS = ('send', 'forward')
# 算进数字人发送上限的动作: 再加上回复云文档评论(走 lark-cli, 不能用 larkx recall 撤回)
RATE_LIMITED_ACTIONS = (*SENT_ACTIONS, 'doc_reply')
FORWARD_MAX = 50


def _audit(st, action, args_chat, content, msg_id='', ok=True, error=''):
    turn = state.read_turn() if is_agent_actor() else None
    st.log_action(actor='agent' if is_agent_actor() else 'human', action=action, chat_id=str(args_chat or ''),
                  msg_id=str(msg_id or ''), content=content, ok=1 if ok else 0, error=error,
                  turn_kind=(turn or {}).get('kind', ''), turn_ref=(turn or {}).get('ref', ''))


def cmd_doc(args):
    """云文档: read 读正文, comment 看一条评论的上下文, reply 在评论下回复(走 lark-cli, 以本人身份)。"""
    from . import doc_comments
    if args.action == 'reply':
        return _doc_reply(args)
    if args.action == 'comment' and not args.comment_id:
        return _fail(args, 'larkx doc comment 要给评论 ID')
    st = Storage()
    try:
        if args.action == 'read':
            content = doc_comments.read(load_config(), st, args.chat_id)
            if args.json:
                _print_json({'ok': True, 'chat_id': args.chat_id, 'title': st.get_chat_name(args.chat_id), 'content': content})
            else:
                print(content)
            return 0
        thread = doc_comments.comment(load_config(), st, args.chat_id, args.comment_id)
    except doc_comments.DocError as e:
        return _fail(args, str(e))
    if args.json:
        _print_json({'ok': True, **thread})
    else:
        print(f"评论 {thread['comment_id']}{'(已解决)' if thread['is_solved'] else ''} 评的原文: {thread['quote'] or '(全文评论)'}")
        for r in thread['replies']:
            print(f"- {r['from']}: {r['text']}")
    return 0


def _doc_reply(args):
    """在云文档评论下以本人身份回复; 管控和 send 一样: 防自回环、只能回触发这一轮的文档、发送上限、签名、抄送。"""
    from . import doc_comments
    from .protocol.proto.decoders import DOC_CHAT_TYPE
    if not args.comment_id:
        return _fail(args, 'larkx doc reply 要给评论 ID')
    text = _read_send_text(args)
    if not text.strip():
        return _fail(args, '回复内容为空')
    cfg = load_config()
    prefix = (cfg.get('triggers') or {}).get('prefix') or ''
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    client = LarkClient(LarkAuth())
    name = st.get_chat_name(args.chat_id) or args.chat_id
    if agent:
        from .daemon.triggers import strip_mentions
        from .notify import Notifier
        denied = f'数字人的回复不能以触发前缀 {prefix} 开头' if prefix and strip_mentions(text).startswith(prefix) else ''
        denied = denied or state.send_denied(args.chat_id, turn)
        if not denied:
            n = st.count_actions('agent', RATE_LIMITED_ACTIONS, datetime.now() - timedelta(minutes=10))
            if n >= cfg['send_rate_limit']:
                denied = f'数字人 10 分钟内已经发了 {n} 条消息, 达到上限 {cfg["send_rate_limit"]}(LARKX_SEND_RATE_LIMIT)'
        if denied:
            _audit(st, 'doc_reply', args.chat_id, text, ok=False, error=denied)
            Notifier(cfg, client).alert(f'拦下了数字人在云文档「{name}」的评论回复: {denied}\n内容: {text[:200]}',
                                        key=f'deny:{args.chat_id}:{denied[:20]}', cooldown=600)
            return _fail(args, denied)
        text = with_signature(text, cfg)
    try:
        reply_id = doc_comments.reply(cfg, st, args.chat_id, args.comment_id, text)
    except doc_comments.DocError as e:
        _audit(st, 'doc_reply', args.chat_id, text, ok=False, error=str(e))
        return _fail(args, str(e))
    _audit(st, 'doc_reply', args.chat_id, text, msg_id=reply_id)
    if agent and cfg.get('mirror'):
        from .notify import Notifier, should_mirror
        notifier = Notifier(cfg, client)
        if should_mirror(args.chat_id, turn, notifier.owner_chat()):
            notifier.send(f'📤 数字人在云文档「{name}」的评论下回复了:\n{text}', fallback=False)
    # 和 send 一样落库, 本地记录里看得到这篇文档下发过什么
    if reply_id:
        st.save_message({'msg_id': reply_id, 'chat_id': args.chat_id, 'chat_type': DOC_CHAT_TYPE, 'scope': 'topic',
                         'anchor': args.comment_id, 'from_id': client.auth.user_id, 'msg_type_name': 'DOC_REPLY',
                         'content': text, 'create_time': int(datetime.now().timestamp())}, sender_name='我', direction='out')
    if args.json:
        _print_json({'ok': True, 'reply_id': reply_id})
    else:
        print(f'已回复 {reply_id}'.rstrip())
    return 0


# 这些后缀、不超过这个大小的文件按图片消息发, 其余按文件消息发
SEND_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
SEND_IMAGE_MAX = 10 * 1024 * 1024


def _send_files(args) -> tuple:
    """--file 给的文件: 返回 ([{path, name, size, mime, image}], 错误)。只查文件本身, 越权由 guest.send_file_denied 查。"""
    import mimetypes
    files = []
    for path in getattr(args, 'file', None) or []:
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(full):
            return [], f'找不到文件(或不是普通文件): {path}'
        size = os.path.getsize(full)
        if size <= 0:
            return [], f'不能发送空文件: {path}'
        name = os.path.basename(full)
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        image = os.path.splitext(name)[1].lower() in SEND_IMAGE_EXTS and size <= SEND_IMAGE_MAX
        files.append({'path': path, 'full': full, 'name': name, 'size': size, 'mime': mime, 'image': image})
    return files, ''


def _file_label(f: dict) -> str:
    return f'[图片] {f["name"]}' if f['image'] else f'[文件] {f["name"]}'


def _send_one_file(client, f: dict, chat_id: str, root, cfg, thread):
    """上传一个文件再发出去, 返回 (消息, msg_id); 上传失败抛 media.UploadError。"""
    from .protocol import media
    if f['image']:
        with open(f['full'], 'rb') as fh:
            key = media.upload_image(client.auth, fh.read(), f['name'], f['mime'])
        msg = {'kind': 'image', 'key': key}
    else:
        with open(f['full'], 'rb') as fh:
            key = media.upload_file(client.auth, fh, f['size'], f['name'], f['mime'])
        msg = {'kind': 'file', 'key': key, 'name': f['name'], 'size': f['size'], 'mime': f['mime']}
    return msg, client.send_message(msg, chat_id, root_id=root, keep_unread=cfg['keep_unread'], in_thread=thread)


def cmd_send(args):
    files, error = _send_files(args)
    if error:
        return _fail(args, error)
    # 只发文件时不读 stdin(没有内容时 stdin 可能一直不结束); 要同时发文字就写在参数里或写 -
    text = _read_send_text(args) if (args.text or not files) else ''
    if not text.strip() and not files:
        return _fail(args, '消息内容为空')
    mentions=[]
    for value in getattr(args, 'mention', []) or []:
        uid, sep, name = value.partition(':')
        if not sep or not uid.isdigit() or not name.strip():
            return _fail(args, '--mention 格式为数字 user_id:显示名')
        mentions.append({'type':'at','user_id':uid,'name':name})
    if mentions and not text.strip():
        return _fail(args, '--mention 要和文字内容一起发')
    has_text = bool(text.strip())
    labels = '\n'.join(_file_label(f) for f in files)
    summary = '\n'.join(x for x in (text, labels) if x)  # 审计、告警里记的内容
    cfg = load_config()
    prefix = (cfg.get('triggers') or {}).get('prefix') or ''
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    client = LarkClient(LarkAuth())
    urgent_type = getattr(args, 'urgent_type', None) or ('app' if getattr(args, 'urgent', False) or getattr(args, 'urgent_to', None) else None)
    urgent_to = []
    if urgent_type:
        # 发出去之前先确认能加急、加急谁, 免得消息发了加急却被拒
        err = _urgent_denied(turn, urgent_type) if agent else ''
        if not err and getattr(args, 'urgent_to', None):
            urgent_to, err = _resolve_urgent_targets(client, args.urgent_to)
        elif not err:
            try:
                peer = client.p2p_peer(args.chat_id)
            except Exception as e:
                peer, err = '', f'查询会话失败: {e}'[:300]
            urgent_to = [peer] if peer else []
            err = err or ('' if peer else '群里加急要用 --urgent-to 指定加急谁(user_id 或名字, 可以写几个)')
        if not err and str(client.auth.user_id) in urgent_to:
            err = '不能加急本人自己'
        if err:
            if agent:
                _audit(st, 'urgent', args.chat_id, f'发送并加急 → {" ".join(urgent_to) or "(默认对象)"}', ok=False, error=err)
            return _fail(args, err + '(消息没有发出)')
    # 防自回环的前缀检测按纯文本做, 不能让 markdown 格式把开头的触发前缀藏起来
    plain = text
    if getattr(args, 'markdown', False):
        from .protocol import richtext
        plain = richtext.to_plain_text(text)
    if agent:
        from . import guest
        from .daemon.triggers import strip_mentions
        from .notify import Notifier
        # 文件先查: 凭证、密钥不能发; 访客这一轮只能发数字人 downloads/ 里的文件
        denied = next((d for d in (guest.send_file_denied(f['path'], cfg, turn) for f in files) if d), '')
        # 允许自己触发时, 以前缀开头的回复会再次触发数字人, 形成自回环
        if not denied and has_text and prefix and strip_mentions(plain).startswith(prefix):
            denied = f'数字人的回复不能以触发前缀 {prefix} 开头'
        denied = denied or state.send_denied(args.chat_id, turn)
        if not denied:
            n = st.count_actions('agent', RATE_LIMITED_ACTIONS, datetime.now() - timedelta(minutes=10))
            count = int(has_text) + len(files)
            if n + count > cfg['send_rate_limit']:
                denied = f'数字人 10 分钟内已经发了 {n} 条消息, 达到上限 {cfg["send_rate_limit"]}(LARKX_SEND_RATE_LIMIT)'
        if denied:
            _audit(st, 'send', args.chat_id, summary, ok=False, error=denied)
            name = st.get_chat_name(args.chat_id) or args.chat_id
            Notifier(cfg, client).alert(f'拦下了数字人发往「{name}」的消息: {denied}\n内容: {summary[:200]}',
                                        key=f'deny:{args.chat_id}:{denied[:20]}', cooldown=600)
            return _fail(args, denied)
        if has_text:
            text = with_signature(text, cfg)
            if getattr(args, 'markdown', False):
                from .protocol import richtext
                plain = richtext.to_plain_text(text)
    # --thread: 普通群里回进以 --root 为根的话题(还没有话题就新开一个), 而不是主会话里的引用回复
    thread = True if getattr(args, 'thread', False) else None
    if thread and not args.root:
        return _fail(args, '--thread 要和 --root 一起用: 指定回进哪条消息的话题')
    markdown = getattr(args, 'markdown', False)
    sent = []  # 发出去的 (msg_id, 抄送里记的内容)
    if has_text:
        if mentions:
            # 正文里写了 "@显示名" 的就地变成 @, 没写到的放在最前面(见 protocol/mentions.py)
            from .protocol import mentions as mention_mod
            from .protocol.proto import builders
            if markdown:
                msg = {'kind': 'markdown', 'text': text, 'mentions': mentions}
            else:
                msg = {'kind': 'text', 'segments': mention_mod.text_segments(text, mentions)}
            msg_id = client.send_message(msg, args.chat_id, root_id=args.root, keep_unread=cfg['keep_unread'],
                                         in_thread=thread)
            # 审计、抄送、落库里的纯文本: @ 写成 "@显示名", 位置和发出去的一致
            if markdown:
                plain = builders.plain_text(msg)
                text = mention_mod.lead_text(mention_mod.markdown_unplaced(text, mentions)) + text
            else:
                text = builders.plain_text(msg)
        elif thread:
            msg_id = client.send_message({'kind': 'markdown' if markdown else 'text', 'text': text}, args.chat_id,
                                         root_id=args.root, keep_unread=cfg['keep_unread'], in_thread=True)
        elif markdown:
            msg_id = client.send_markdown(text, args.chat_id, root_id=args.root, keep_unread=cfg['keep_unread'])
        else:
            msg_id = client.send_msg(text, args.chat_id, root_id=args.root, keep_unread=cfg['keep_unread'])
        _audit(st, 'send', args.chat_id, text, msg_id=msg_id, ok=bool(msg_id), error='' if msg_id else '发送失败')
        if not msg_id:
            return _fail(args, '发送失败')
        sent.append((msg_id, text))
        # 用服务端 id 落库,监听收到回显时会补全这条而不是再插一条
        st.save_message({'msg_id': msg_id, 'chat_id': args.chat_id, 'chat_type': 0, 'scope': 'topic' if args.root else 'chat', 'anchor': args.root or '', 'from_id': client.auth.user_id, 'msg_type': 2 if getattr(args, 'markdown', False) else 4, 'msg_type_name': 'POST' if getattr(args, 'markdown', False) else 'TEXT', 'content': plain if getattr(args, 'markdown', False) else text, 'content_data': {'_larkx_mention_ids': [m['user_id'] for m in mentions]}, 'create_time': int(datetime.now().timestamp())}, sender_name='我', direction='out')
    error = ''
    for f in files:
        label = _file_label(f)
        try:
            msg, msg_id = _send_one_file(client, f, args.chat_id, args.root, cfg, thread)
            error = '' if msg_id else f'{f["name"]} 发送失败'
        except Exception as e:
            msg, msg_id, error = None, None, f'上传 {f["name"]} 失败: {e}'[:300]
        _audit(st, 'send', args.chat_id, label, msg_id=msg_id or '', ok=bool(msg_id), error=error)
        if not msg_id:
            break
        sent.append((msg_id, label))
        if msg['kind'] == 'image':
            data, mtype = {'imageV2': {'imageKey': msg['key']}}, (5, 'IMAGE')
        else:
            data, mtype = {k: msg[k] for k in ('key', 'name', 'size', 'mime')}, (3, 'FILE')
        st.save_message({'msg_id': msg_id, 'chat_id': args.chat_id, 'chat_type': 0, 'scope': 'topic' if args.root else 'chat', 'anchor': args.root or '', 'from_id': client.auth.user_id, 'msg_type': mtype[0], 'msg_type_name': mtype[1], 'content': label, 'content_data': data, 'create_time': int(datetime.now().timestamp())}, sender_name='我', direction='out')
    if agent and cfg.get('mirror') and sent:
        from .notify import Notifier, should_mirror
        notifier = Notifier(cfg, client)
        if should_mirror(args.chat_id, turn, notifier.owner_chat()):
            name = st.get_chat_name(args.chat_id) or args.chat_id
            notifier.send(f'📤 数字人在「{name}」发了:\n' + '\n'.join(c for _, c in sent), fallback=False)
    ids = [m for m, _ in sent]
    if error:
        if args.json:
            _print_json({'ok': False, 'error': error, 'msg_ids': ids, 'chat_id': args.chat_id})
        else:
            print(f'{error}' + (f'; 已发出 msg_id={" ".join(ids)}' if ids else ''), file=sys.stderr)
        return 1
    msg_id = ids[0]
    urgent, urgent_err = None, ''
    if urgent_type:
        # 加急发出的第一条(有文字时是文字那条)
        mine = {'msg_id': msg_id, 'chat_id': args.chat_id, 'from_id': client.auth.user_id, 'recalled': False}
        urgent, urgent_err = _do_urgent(args, client, st, cfg, msg_id, urgent_to, urgent_type, msg=mine)
    if args.json:
        out = {'ok': not urgent_err, 'msg_id': msg_id, 'chat_id': args.chat_id}
        if files:
            out['msg_ids'] = ids   # 带了文件时每条消息的 id 按发出顺序
        if urgent_type:
            out['urgent'] = urgent
        if urgent_err:
            out['error'] = f'消息已发出, 加急没成功: {urgent_err}'
        _print_json(out)
    else:
        print(f'已发送 msg_id={" ".join(ids)}')
        if urgent:
            print(f'已{URGENT_TYPE_NAMES[urgent_type]}加急 → {" ".join(urgent["urgent"]) or "(无)"}')
        if urgent_err:
            print(f'消息已发出, 加急没成功: {urgent_err}', file=sys.stderr)
    return 1 if urgent_err else 0


def _forward_denied(st, cfg, sources: list, targets: list, turn, count: int, merge: bool) -> str:
    """数字人这次转发是否越权, 规则和 larkx send 一样; 越权时返回原因, 允许时返回空串。count 是会发出的消息条数。"""
    for t in targets:
        denied = state.send_denied(t, turn)
        if denied:
            return denied
    if state.is_guest(turn) and any(str(m['chat_id']) != turn['chat_id'] for m in sources):
        return '访客发起的请求只能转发当前会话里的消息'
    prefix = (cfg.get('triggers') or {}).get('prefix') or ''
    if prefix and not merge:
        # 逐条转发出去的消息内容不变, 和 send 一样不能以触发前缀开头(合并转发是一条"聊天记录", 不会触发)
        from .daemon.triggers import strip_mentions
        if any(strip_mentions(m['content']).startswith(prefix) for m in sources):
            return f'数字人不能逐条转发以触发前缀 {prefix} 开头的消息'
    n = st.count_actions('agent', RATE_LIMITED_ACTIONS, datetime.now() - timedelta(minutes=10))
    if n + count > cfg['send_rate_limit']:
        return (f'数字人 10 分钟内已经发了 {n} 条消息, 再转发 {count} 条会超过上限 {cfg["send_rate_limit"]}'
                '(LARKX_SEND_RATE_LIMIT)')
    return ''


def _forward_text(group: list, found: dict, merge: bool) -> str:
    """审计和抄送里记的内容: 转的是哪几条、各是什么。"""
    lines = [f'{found[i]["content"] or ""}'.replace('\n', ' ')[:200] for i in group]
    if merge:
        return f'[合并转发 {len(group)} 条: {" ".join(group)}]\n' + '\n'.join(lines)
    return f'[转发 {group[0]}] {lines[0]}'


def cmd_forward(args):
    ids = list(dict.fromkeys(str(i).strip() for i in args.msg_id))
    targets = list(dict.fromkeys(str(t).strip() for t in args.to or []))
    if not targets:
        return _fail(args, '用 --to <chat_id> 指定转发到哪个会话, 可以写几个')
    bad = [i for i in ids + targets if not i.isdigit()]
    if bad:
        return _fail(args, f'消息 id 和会话 id 都是数字(larkx messages / history 里的 msg_id, larkx chats 里的 chat_id), '
                           f'这些不对: {" ".join(bad)}')
    if len(ids) > FORWARD_MAX:
        return _fail(args, f'一次最多转发 {FORWARD_MAX} 条')
    merge = bool(getattr(args, 'merge', False))
    cfg = load_config()
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    client = LarkClient(LarkAuth())
    try:
        found = client.get_messages(ids)
    except Exception as e:
        return _fail(args, f'查询要转发的消息失败: {e}'[:300])
    missing = [i for i in ids if i not in found or found[i].get('recalled')]
    if missing:
        return _fail(args, f'找不到这些消息(不存在、已撤回, 或者本人看不到): {" ".join(missing)}')
    sources = [found[i] for i in ids]
    if merge and len({str(m['chat_id']) for m in sources}) > 1:
        return _fail(args, '合并转发的消息必须来自同一个会话; 不同会话的消息分开合并, 或者逐条转发')
    groups = [ids] if merge else [[i] for i in ids]
    if agent:
        denied = _forward_denied(st, cfg, sources, targets, turn, len(groups) * len(targets), merge)
        if denied:
            names = '」「'.join(st.get_chat_name(t) or t for t in targets)
            text = '\n'.join(_forward_text(g, found, merge) for g in groups)
            _audit(st, 'forward', targets[0] if len(targets) == 1 else '', f'转发到 {" ".join(targets)}\n{text}',
                   ok=False, error=denied)
            from .notify import Notifier
            Notifier(cfg, client).alert(f'拦下了数字人往「{names}」转发的 {len(ids)} 条消息: {denied}\n内容: {text[:200]}',
                                        key=f'deny-forward:{targets[0]}:{denied[:20]}', cooldown=600)
            return _fail(args, denied)
    done, failed = [], []
    for group in groups:
        try:
            got = client.merge_forward(group, targets) if merge else client.forward_message(group[0], targets)
            error = '没有拿到目标会话里新消息的 id, 可能没有发出'
        except Exception as e:
            got, error = {}, f'转发失败: {e}'[:300]
        text = _forward_text(group, found, merge)
        for t in targets:
            m = got.get(t)
            _audit(st, 'forward', t, text, msg_id=m['msg_id'] if m else '', ok=bool(m), error='' if m else error)
            if not m:
                failed.append({'chat_id': t, 'source_ids': group, 'error': error})
                continue
            done.append({'chat_id': t, 'msg_id': m['msg_id'], 'source_ids': group})
            # 先落库(不带 position), 监听收到回显时补全这条; 回显是登记过的转发, daemon 只入库, 不当成本人的指令
            st.save_message(dict(m, position=0), sender_name='我', direction='out')
    if agent and cfg.get('mirror') and done:
        from .notify import Notifier, should_mirror
        notifier = Notifier(cfg, client)
        owner = notifier.owner_chat()
        for t in targets:
            sent = [d for d in done if d['chat_id'] == t]
            if sent and should_mirror(t, turn, owner):
                name = st.get_chat_name(t) or t
                text = '\n'.join(_forward_text(d['source_ids'], found, merge) for d in sent)
                n = len(ids) if merge else len(sent)
                notifier.send(f'📤 数字人往「{name}」{"合并" if merge else ""}转发了 {n} 条消息:\n{text}', fallback=False)
    if args.json:
        _print_json({'ok': not failed, 'merge': merge, 'messages': done, 'failed': failed})
    else:
        for d in done:
            what = f'已合并转发 {len(ids)} 条' if merge else f'已转发 {d["source_ids"][0]}'
            print(f'{what} → {d["chat_id"]}: msg_id={d["msg_id"]}')
        for f in failed:
            print(f'没有转发出去 {" ".join(f["source_ids"])} → {f["chat_id"]}: {f["error"]}', file=sys.stderr)
    return 1 if failed else 0


def cmd_config(args):
    cfg = load_config()
    try:
        from sqlalchemy.engine import make_url
        cfg['storage_url'] = make_url(cfg['storage_url']).render_as_string(hide_password=True)
    except Exception:
        cfg['storage_url'] = '(无法解析)'
    _print_json(cfg)
    return 0


def _chat_tags(r: dict) -> str:
    tags = [r['attention_name'] + ('' if r['attention_source'] in ('跟分类', '单独设置') else '·建议')]
    tags += (['私人'] if r['private'] else []) + (['免打扰'] if r['feishu_muted'] else []) + \
        (['置顶'] if r['feishu_pinned'] else []) + (['不清理'] if r.get('kept') else [])
    return ' '.join(f'[{t}]' for t in tags)


def cmd_chats(args):
    from . import groups
    st = Storage()
    chats = st.list_chats(limit=100000)
    rows = {r['chat_id']: r for r in groups.ChatStore(st).view()}
    # 会话列表本身的字段保持原样(updated_at 仍是库里的时间), 分类和关注度只是附加字段
    rows = [dict(rows[c.chat_id], name=c.name, chat_type=c.chat_type, updated_at=c.updated_at) for c in chats
            if c.chat_id in rows]
    try:
        if args.attention:
            want = {groups.parse_attention(a) for a in args.attention.split(',') if a.strip()}
            rows = [r for r in rows if r['attention'] in want]
        if args.active:
            since = time.time() - state.parse_duration(args.active)
            rows = [r for r in rows if (r['last_at'] or 0) >= since]
    except ValueError as e:
        return _fail(args, str(e))
    if args.group:
        rows = [r for r in rows if (r['group'] or r['suggested_group']) == args.group]
    if args.feishu_muted:
        rows = [r for r in rows if r['feishu_muted']]
    rows = rows[:args.limit]
    if args.json:
        _print_json(rows)
        return 0
    grouped = load_config().get('chat_groups')
    for r in rows:
        type_name = {1: '私聊', 2: '群聊', 3: '话题群'}.get(r['chat_type'], '?')
        if not grouped:  # 没开会话分类: 输出和原来一样
            print(f"{r['chat_id']}\t{type_name}\t{r['name'] or '(未命名)'}\t更新于 {r['updated_at']}")
            continue
        group = r['group'] or (f"{r['suggested_group']}?" if r['suggested_group'] else '未分类')
        print(f"{r['chat_id']}\t{type_name}\t{r['name'] or '(未命名)'}\t{group}\t{_chat_tags(r)}\t更新于 {r['updated_at']}")
    return 0


def _owner_turn_denied() -> str:
    """数字人只有在本人发起的轮次里才能改分类(和监听任务一样)。"""
    if not is_agent_actor():
        return ''
    turn = state.read_turn()
    if turn and turn.get('is_owner') and turn.get('kind') in ('message', 'say'):
        return ''
    return '只有本人发起时才能调整会话分类'


def _resolve_chats(args, cs, targets) -> list:
    """会话名或 chat_id -> chat_id 列表; 有一个找不到或对不上唯一会话就整体失败(返回 None)。"""
    out = []
    for t in targets:
        found = cs.find_chats(t)
        if len(found) == 1:
            out.append(found[0][0])
            continue
        if not found:
            _fail(args, f'本地没有叫「{t}」的会话(可以用 larkx chats 查, 或等它有新消息、先回填历史)')
            return None
        if _interactive() and not args.json:
            i = _choose(f'「{t}」对上 {len(found)} 个会话, 选一个:', [f'{n} ({c})' for c, n in found])
            if i is None:
                return None
            out.append(found[i][0])
            continue
        _fail(args, f'「{t}」对上 {len(found)} 个会话, 用 chat_id 重新运行: ' + '; '.join(f'{n} ({c})' for c, n in found[:10]))
        return None
    return out


def cmd_groups(args):
    """会话分类: 看分类和成员数、建分类、改分类(关注度、关键词、私人)、删分类、把会话归进分类、刷新画像。"""
    from . import groups
    st = Storage()
    cs = groups.ChatStore(st)
    if args.action == 'list':
        views = cs.view()
        counts = {}
        for r in views:
            counts[r['group_id']] = counts.get(r['group_id'], 0) + 1
        rows = [{'id': g.id, 'name': g.name, 'attention': g.attention, 'attention_name': groups.ATTENTION[g.attention],
                 'private': bool(g.private), 'keywords': json.loads(g.keywords or '[]'), 'chats': counts.get(g.id, 0)} for g in cs.groups()]
        unassigned = counts.get(0, 0)
        if args.json:
            _print_json({'groups': rows, 'unassigned': unassigned})
            return 0
        for g in rows:
            extra = (' 私人' if g['private'] else '') + (f" 关键词: {', '.join(g['keywords'])}" if g['keywords'] else '')
            print(f"{g['name']}\t{g['attention_name']}\t{g['chats']} 个会话{extra}")
        print(f'未分类: {unassigned} 个会话(按画像建议生效, larkx chats 查看)')
        return 0
    denied = _owner_turn_denied()
    if denied:
        return _fail(args, denied, code=3)
    try:
        attention = None
        if args.attention:
            attention = '' if args.attention in groups.FOLLOW_GROUP else groups.parse_attention(args.attention)
        keywords = [k.strip() for k in args.keywords.split(',') if k.strip()] if args.keywords is not None else None
        if args.action == 'profile':
            if inside_twin():
                return _fail(args, '数字人不能刷新画像, 它每天会自动跑', code=3)
            res = groups.profile_all(LarkClient(LarkAuth()), st, _owner_id(), action_words=groups.action_words(load_config()))
            msg = f"已刷新 {res['chats']} 个会话的画像; 新会话 {len(res['new'])} 个, 建议有变化 {res['changed']} 个"
            return (_print_json({'ok': True, **res}) or 0) if args.json else (print(msg) or 0)
        if not args.targets:
            return _fail(args, f'用法: larkx groups {args.action} <名字>')
        if args.action == 'add':
            g = cs.create_group(args.targets[0], attention or 'light', keywords or [], bool(args.private))
            _audit(st, 'group', '', f'建分类 {g.name} {groups.ATTENTION[g.attention]}')
            msg = f'已建分类「{g.name}」({groups.ATTENTION[g.attention]}), 立即生效'
        elif args.action in ('edit', 'remove'):
            g = cs.group(args.targets[0])
            if not g:
                return _fail(args, f'没有分类「{args.targets[0]}」, 用 larkx groups 查看')
            if args.action == 'remove':
                n = cs.delete_group(g.id)
                _audit(st, 'group', '', f'删分类 {g.name}')
                msg = f'已删分类「{g.name}」, {n} 个会话变成未分类'
            else:
                fields = {k: v for k, v in (('name', args.name), ('attention', attention or None),
                                             ('keywords', keywords)) if v is not None}
                if args.private is not None:
                    fields['private'] = 1 if args.private else 0
                if not fields:
                    return _fail(args, '要改什么? --name / --attention / --keywords / --private / --no-private')
                g = cs.update_group(g.id, **fields)
                _audit(st, 'group', '', f'改分类 {g.name}: {fields}')
                msg = f'已更新分类「{g.name}」, 立即生效'
        else:  # assign
            if args.to is None and attention is None:
                return _fail(args, '要指定 --to <分类名> 或 --attention <关注度|跟分类>')
            group_id = None
            if args.to is not None:
                g = cs.group(args.to) if args.to else None
                if args.to and not g:
                    return _fail(args, f'没有分类「{args.to}」, 先 larkx groups add {args.to}')
                group_id = g.id if g else 0
            ids = _resolve_chats(args, cs, args.targets)
            if ids is None:
                return 1
            cs.assign(ids, group_id=group_id, attention=attention)
            names = '、'.join(r['name'] or r['chat_id'] for r in cs.view(ids))
            _audit(st, 'group', ','.join(ids), f'归类 {names} -> {args.to or "-"} {attention or ""}')
            msg = f'已调整 {names}, 立即生效'
    except ValueError as e:
        return _fail(args, str(e))
    return (_print_json({'ok': True, 'message': msg}) or 0) if args.json else (print(msg) or 0)


def _since_ts(text: str, now: float) -> int:
    """'1h' / '30m' 这类时长(往前推), 或 unix 秒。"""
    text = str(text).strip()
    return int(text) if text.isdigit() and len(text) >= 9 else int(now - state.parse_duration(text))


def cmd_sessions(args):
    st = Storage()
    rows = st.list_sessions(limit=args.limit)
    for r in rows:
        scope = '话题' if r.anchor else ('全局' if r.session_key == 'global' else '会话')
        started = '已建立' if r.started else '待建立'
        print(f"{r.session_key}\t{scope}\tchat={r.chat_id}\tanchor={r.anchor or '-'}\t{started}\tmsg={r.msg_count}\t活跃于 {r.last_active}")
    return 0


def cmd_messages(args):
    st = Storage()
    anchor = None
    if args.anchor is not None:
        anchor = args.anchor
    since = None
    if args.since:
        try:
            since = int(time.time() - state.parse_duration(args.since))
        except ValueError as e:
            return _fail(args, str(e))
    rows = st.get_messages(args.chat_id, anchor=anchor, limit=args.limit, since_ts=since)
    if args.json:
        _print_json([{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows])
        return 0
    for r in rows:
        ts = datetime.fromtimestamp(r.create_time).strftime('%m-%d %H:%M') if r.create_time else ''
        scope = f'[{r.scope}:{r.anchor[:8]}]' if r.anchor else ''
        content = r.content or ''
        shown = content[:120] + ('…' if len(content) > 120 else '')
        print(f'[{ts}]{scope} {r.sender_name or r.sender_id}: {shown}')
    return 0


def cmd_search(args):
    client = LarkClient(LarkAuth())
    results = client.search(' '.join(args.query))
    _print_json(results)
    return 0


def cmd_api(args):
    client = LarkClient(LarkAuth())
    data = None
    if args.json_body:
        try:
            data = json.loads(args.json_body)
        except Exception as e:
            print(f'JSON 解析失败: {e}', file=sys.stderr)
            return 1
    try:
        resp = client.api(args.type, data)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    _print_json(resp)
    return 0


def cmd_history(args):
    client = LarkClient(LarkAuth())
    chat_id = args.chat_id
    if args.start is not None:
        start = args.start
    else:
        info = client.get_chat_info(chat_id)
        if not info or not info.get('lastMessagePosition'):
            print('拿不到会话的最新 position,请用 --start 指定', file=sys.stderr)
            return 1
        start = int(info['lastMessagePosition']) - args.count + 1
        if info.get('name'):
            st = Storage()
            st.update_chat_name(chat_id, info['name'], info.get('type', 0) if isinstance(info.get('type'), int) else 0)
    positions = list(range(max(1, start), max(1, start) + args.count))
    st = Storage() if args.save else None
    msgs = client.pull_history(chat_id, positions, save_storage=st)
    for m in msgs:
        if args.json:
            continue
        ts = datetime.fromtimestamp(m['create_time']).strftime('%m-%d %H:%M') if m.get('create_time') else ''
        scope = f"[{m['scope']}:{m['anchor'][:8]}]" if m['anchor'] else ''
        content = m['content'] or ''
        shown = content[:120] + ('…' if len(content) > 120 else '')
        print(f"{m['position']:>6} [{ts}]{scope} {m['from_id']}: {shown}")
    if args.json:
        _print_json([{k: v for k, v in m.items() if k != 'content_data'} for m in msgs])
    if args.save:
        print(f'已入库 {len(msgs)} 条')
    return 0


def cmd_download(args):
    from .protocol.media import download, extract_resource_key, message_resource_url
    auth = LarkAuth()
    auth.require_valid()
    file_host = auth.region.file_host
    if args.key:
        if not args.chat:
            print('--key 需要配合 --chat <chat_id>', file=sys.stderr)
            return 1
        url = message_resource_url(args.msg_id or '0', args.key, args.chat, host=file_host)
        msg = None
    else:
        st = Storage()
        rows = None
        if args.chat:
            rows = st.get_messages(args.chat, limit=500)
            rows = [r for r in rows if r.msg_id == args.msg_id]
        else:
            from .storage.models import Message
            s = st.Session()
            rows = s.query(Message).filter_by(msg_id=args.msg_id).all()
            s.close()
        if not rows:
            print(f'本地未找到消息 {args.msg_id}(可用 --key/--chat 直接指定)', file=sys.stderr)
            return 1
        r = rows[-1]
        msg = {'msg_id': r.msg_id, 'chat_id': r.chat_id, 'msg_type': r.msg_type, 'content_data': json.loads(r.content_data) if r.content_data else None}
        key = extract_resource_key(r.msg_type, msg['content_data'] or {})
        if not key:
            print(f'消息 {args.msg_id} 类型为 {r.msg_type_name},没有可下载资源', file=sys.stderr)
            return 1
        url = message_resource_url(r.msg_id, key, r.chat_id, host=file_host)
    out = args.out
    if not out:
        print('必须指定 -o/--out 保存路径', file=sys.stderr)
        return 1
    path, content_type, n = download(auth, url, out)
    print(f'已下载: {path} ({content_type}, {n} 字节)')
    return 0


def _run_logged_in(fn, need_user_id=False):
    try:
        while True:
            try:
                auth = _interactive_login()
                if need_user_id and not auth.user_id:
                    auth._fetch_profile()
                    auth.save()
                if need_user_id and not auth.user_id:
                    print('无法确定当前账号 user_id, 数字人存在自回环风险, 已拒绝启动。请先 larkx auth check 确认凭证有效。', file=sys.stderr)
                    return 1
                return fn(auth)
            except AuthExpired as e:
                if not sys.stdin.isatty():
                    print(str(e), file=sys.stderr)
                    return 1
                print(f'运行中凭证失效: {e}', flush=True)
    except KeyboardInterrupt:
        print('\n已停止')
        return 0


def cmd_listen(args):
    from .daemon import run_listen
    return _run_logged_in(run_listen)


def cmd_daemon(args):
    from .daemon import run_daemon, run_service
    if not sys.stdin.isatty():
        # 无人值守: 登录失效时告警并等待重新登录, 不让 systemd 反复重启
        return run_service()
    return _run_logged_in(run_daemon, need_user_id=True)


def _task_row(t) -> dict:
    return {'id': t.id, 'kind': t.kind, 'status': t.status, 'due_at': datetime.fromtimestamp(t.due_at).strftime('%Y-%m-%d %H:%M:%S') if t.due_at else '',
            'every_sec': t.every_sec, 'chat_id': t.chat_id, 'anchor': t.anchor, 'by_owner': bool(t.by_owner),
            'creator': t.creator, 'run_count': t.run_count, 'prompt': t.prompt, 'result': t.result}


def cmd_recall(args):
    st = Storage()
    sent = st.find_action('agent', SENT_ACTIONS, args.msg_id) or st.find_action('human', SENT_ACTIONS, args.msg_id)
    if is_agent_actor():
        turn = state.read_turn()
        mine = st.find_action('agent', SENT_ACTIONS, args.msg_id)
        if not mine:
            return _fail(args, '数字人只能撤回自己发出的消息')
        if not turn or turn['kind'] == 'system' or (not turn['is_owner'] and mine.chat_id != turn['chat_id']):
            return _fail(args, '只能撤回当前会话里自己发的消息; 其他会话的需要本人发起')
    client = LarkClient(LarkAuth())
    chat_id = sent.chat_id if sent else ''
    try:
        client.api('messages.DeleteMessageRequest', {'id': str(args.msg_id)})
    except Exception as e:
        err = f'撤回失败: {e}'[:300]
        _audit(st, 'recall', chat_id, f'撤回 {args.msg_id}', msg_id=args.msg_id, ok=False, error=err)
        return _fail(args, err + '(只能撤回自己发的消息, 且要在飞书允许的撤回时限内)')
    _audit(st, 'recall', chat_id, f'撤回 {args.msg_id}', msg_id=args.msg_id)
    if args.json:
        _print_json({'ok': True, 'msg_id': args.msg_id})
    else:
        print(f'已撤回 {args.msg_id}')
    return 0


URGENT_TYPE_NAMES = {'app': '应用内', 'sms': '短信', 'phone': '电话'}


def _urgent_denied(turn, urgent_type: str) -> str:
    """数字人这次加急是否越权; 越权时返回原因。规则同跨会话发消息: 只有本人发起的一轮(本人的消息、终端 say)能加急,
    访客、维护任务、汇报都不行; 短信、电话可能计费, 数字人一律不能用。"""
    if urgent_type != 'app':
        return f'数字人不能发{URGENT_TYPE_NAMES[urgent_type]}加急(可能计费), 需要本人在终端里执行'
    if not turn:
        return '当前没有正在处理的消息, 数字人不能加急'
    if turn.get('kind') == 'system':
        return '维护任务里不能加急'
    if turn.get('kind') == 'report':
        return '汇报里不能加急'
    if turn.get('kind') == 'say' or (turn.get('is_owner') and not turn.get('untrusted')):
        return ''
    return '加急需要本人发起'


def _resolve_urgent_targets(client, targets: list) -> tuple:
    """--to 里的 user_id 或名字 → ([user_id], 错误)。名字要能唯一确定一个人, 否则报错并列出候选。"""
    from . import whitelist as wl
    out = []
    for t in targets:
        t = str(t).strip()
        if t.isdigit():  # 名字不会是纯数字
            out.append(t)
            continue
        people = wl.search_people(client, t)
        exact = [p for p in people if p['name'] == t]
        if len(exact) == 1:
            out.append(exact[0]['user_id'])
            continue
        if not people:
            return [], f'没有搜到叫「{t}」的人'
        cands = '; '.join(wl.label(p) for p in (exact or people)[:5])
        return [], f'「{t}」对应不到唯一的人, 请改用 user_id: {cands}'
    return list(dict.fromkeys(out)), ''


def _do_urgent(args, client, st, cfg, msg_id: str, targets: list, urgent_type: str, msg=None) -> tuple:
    """对本人发出的 msg_id 加急; 校验、越权检查、审计都在这里。返回 (结果 dict, 错误)。msg: 已经查过的这条消息。"""
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    kind = URGENT_TYPE_NAMES[urgent_type]
    if agent:
        denied = _urgent_denied(turn, urgent_type)
        if denied:
            _audit(st, 'urgent', '', f'{kind}加急 {msg_id} → {" ".join(targets) or "(默认对象)"}', msg_id=msg_id,
                   ok=False, error=denied)
            from .notify import Notifier
            Notifier(cfg, client).alert(f'拦下了数字人对消息 {msg_id} 的{kind}加急: {denied}',
                                        key=f'deny-urgent:{msg_id}:{denied[:20]}', cooldown=600)
            return None, denied
    if msg is None:
        try:
            msg = client.get_messages([msg_id]).get(msg_id)
        except Exception as e:
            return None, f'查询消息失败: {e}'[:300]
    if not msg or msg.get('recalled'):
        return None, f'找不到消息 {msg_id}(不存在、已撤回, 或者本人看不到)'
    if str(msg.get('from_id') or '') != str(client.auth.user_id):
        return None, '只能加急本人自己发出的消息'
    chat_id = str(msg.get('chat_id') or '')
    if targets:
        ids, err = _resolve_urgent_targets(client, targets)
        if err:
            return None, err
    else:
        try:
            peer = client.p2p_peer(chat_id)
        except Exception as e:
            return None, f'查询会话失败: {e}'[:300]
        if not peer:
            return None, '群里加急要用 --to 指定加急谁(user_id 或名字, 可以写几个)'
        ids = [peer]
    if str(client.auth.user_id) in ids:
        return None, '不能加急本人自己'
    content = f'{kind}加急 {msg_id} → {" ".join(ids)}'
    try:
        res = client.put_urgent(msg_id, ids, urgent_type, chat_id=chat_id)
    except Exception as e:
        err = f'加急失败: {e}'[:300]
        _audit(st, 'urgent', chat_id, content, msg_id=msg_id, ok=False, error=err)
        return None, err
    missed = [i for i in ids if i not in res['chatter_ids']]
    error = f'这些人没加急上: {" ".join(missed)}' if missed else ''
    _audit(st, 'urgent', chat_id, content, msg_id=msg_id, ok=not missed, error=error)
    if agent and cfg.get('mirror'):
        from .notify import Notifier, should_mirror
        notifier = Notifier(cfg, client)
        if should_mirror(chat_id, turn, notifier.owner_chat()):
            name = st.get_chat_name(chat_id) or chat_id
            notifier.send(f'⏰ 数字人在「{name}」对消息 {msg_id} 发了{kind}加急: {" ".join(ids)}', fallback=False)
    return {'msg_id': msg_id, 'chat_id': chat_id, 'type': urgent_type, 'to': ids, 'urgent': res['chatter_ids'],
            'missed': missed, 'invisible': res['invisible_chatter_ids']}, error


def cmd_urgent(args):
    """加急(飞书 buzz): 对本人发出的一条消息发应用内 / 短信 / 电话提醒。

    已读: urgent.PutUrgentRequest 只带本人自己的消息 id、被加急的人、类型和会话 id, 没有已读位置字段, 也不拉取别人的消息;
    实测(2026-11-06, 机器人私聊 7690645243804273642): 加急前后本人在会话里的 readPosition 不变(只有 send 本身把它推到了
    自己那条)。当时会话里没有未读, 没测到"有未读时加急"的情形。消息撤回后加急随之结束, status 里显示为已确认。
    """
    target = list(args.target)
    if target and target[0] == 'status':
        return _urgent_status(args, target[1:])
    if len(target) != 1 or not target[0].isdigit():
        return _fail(args, '用法: larkx urgent <msg_id> [--to <user_id 或名字> ...] [--type app|sms|phone]; '
                           'larkx urgent status <msg_id> ...(msg_id 是数字)')
    client = LarkClient(LarkAuth())
    res, err = _do_urgent(args, client, Storage(), load_config(), target[0], list(args.to or []), args.type)
    if res is None:
        return _fail(args, err)
    if args.json:
        _print_json({'ok': not err, **res, **({'error': err} if err else {})})
    else:
        print(f'已{URGENT_TYPE_NAMES[res["type"]]}加急 {res["msg_id"]} → {" ".join(res["urgent"]) or "(无)"}')
        if err:
            print(err, file=sys.stderr)
    return 1 if err else 0


def _urgent_status(args, ids: list) -> int:
    ids = [str(i).strip() for i in ids]
    if not ids or any(not i.isdigit() for i in ids):
        return _fail(args, '用法: larkx urgent status <msg_id> ...(msg_id 是数字)')
    try:
        got = LarkClient(LarkAuth()).urgent_status(ids)
    except Exception as e:
        return _fail(args, f'查询加急状态失败: {e}'[:300])
    rows = [{'msg_id': i, 'urgent': bool(got.get(i)), 'acks': got.get(i) or []} for i in ids]
    if args.json:
        _print_json({'ok': True, 'messages': rows})
        return 0
    for r in rows:
        if not r['urgent']:
            print(f'{r["msg_id"]}: 没有加急记录')
            continue
        for a in r['acks']:
            when = f'{datetime.fromtimestamp(a["confirm_time"]):%m-%d %H:%M} 已确认' if a['acked'] else '未确认'
            print(f'{r["msg_id"]} → {a["to"]}: {URGENT_TYPE_NAMES.get(a["type"], a["type"])}加急, {when}')
    return 0


def cmd_takeover(args):
    import time
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    if args.action == 'list':
        rows = [{'chat_id': t.chat_id, 'chat_name': st.get_chat_name(t.chat_id),
                 'until': datetime.fromtimestamp(t.until).strftime('%Y-%m-%d %H:%M:%S'), 'goal': t.goal, 'creator': t.creator}
                for t in st.takeover_list() if t.until > time.time()]
        if args.json:
            _print_json(rows)
        else:
            for r in rows:
                print(f"{r['chat_name'] or r['chat_id']}  到 {r['until']}  {r['goal']}")
            if not rows:
                print('当前没有接管中的会话')
        return 0
    if not args.chat_id:
        return _fail(args, f'用法: larkx takeover {args.action} <chat_id>')
    if agent and not (turn and turn['is_owner']) and not (args.action == 'stop' and turn and turn['chat_id'] == args.chat_id):
        return _fail(args, '接管会话需要本人发起')
    if args.action == 'stop':
        ok = st.takeover_expire(args.chat_id, time.time())
        msg = '已结束接管, daemon 会在本人私聊发一份总结' if ok else '这个会话当前没有在接管'
    else:
        try:
            seconds = state.parse_duration(args.for_) if args.for_ else load_config()['takeover_default']
        except ValueError as e:
            return _fail(args, str(e))
        until = time.time() + seconds
        st.takeover_start(args.chat_id, until, args.goal or '', 'agent' if agent else 'human')
        ok, msg = True, f"已接管 {st.get_chat_name(args.chat_id) or args.chat_id} 到 {datetime.fromtimestamp(until):%H:%M}"
    if args.json:
        _print_json({'ok': ok, 'message': msg})
    else:
        print(msg)
    return 0 if ok else 1


def cmd_rule(args):
    from .event_rules import RuleStore
    turn=state.read_turn() if is_agent_actor() else None
    if is_agent_actor() and not (turn and turn.get('is_owner') and turn.get('kind') in ('message','say')):
        return _fail(args, '监听规则只能由本人在对话或终端中管理；事件任务和访客不能管理')
    store=RuleStore(Storage())
    try:
        if args.action=='list':
            _print_json(store.list())
        elif args.action=='runs':
            _print_json(store.history())
        elif args.action=='add':
            if not args.name or not args.chat or not args.instruction:
                return _fail(args, 'add 需要 --name、--chat 和 --instruction（- 从 stdin 读）')
            instruction=sys.stdin.read() if args.instruction=='-' else args.instruction
            rid=store.add(args.name,args.chat,instruction,args.sender,args.mention,args.keyword)
            _print_json({'ok':True,'id':rid,'status':'draft','message':'规则已保存为草稿，请本人确认后执行 rule enable <id>'})
        else:
            if args.id is None: return _fail(args, '需要规则 id')
            if args.action=='enable': store.enable(args.id)
            else: store.pause(args.id,delete=args.action=='delete')
            _print_json({'ok':True,'id':args.id,'action':args.action})
    except ValueError as e:
        return _fail(args,str(e))
    return 0


def cmd_ping(args):
    """数字人判断某件事要本人亲自处理: 记进本人收件箱(state/owner_inbox.jsonl), 本人那边实时能看到。"""
    from . import owner_inbox
    text = ' '.join(args.text).strip()
    if text in ('', '-'):
        text = sys.stdin.read().strip()
    if not text:
        return _fail(args, '要写清楚需要本人处理什么')
    turn = state.read_turn() or {}
    chat_id = args.chat or turn.get('chat_id') or ''
    msg_id = args.msg or (turn.get('ref') if turn.get('kind') == 'message' else '') or ''
    st = Storage()
    entry = owner_inbox.append('ping', text, chat_id=chat_id, chat_name=st.get_chat_name(chat_id) or '' if chat_id else '',
                               msg_id=msg_id, root_id=turn.get('anchor') or '')
    if args.json:
        _print_json(entry)
    else:
        print('已通知本人')
    return 0


def cmd_inbox(args):
    """本人收件箱: 数字人判断要本人处理的事、别人 @ 本人的消息。"""
    from . import owner_inbox
    items = owner_inbox.recent(_since_ts(args.since, time.time()))
    if args.json:
        _print_json(items)
        return 0
    for e in items:
        print(owner_inbox.format_entry(e))
    if not items:
        print('没有')
    return 0


def cmd_watch(args):
    """本人在线: 实时打印收件箱新增的条目; 运行期间别人发起的轮次数字人只判断要不要本人处理(larkx ping), 不回复、不执行。
    Ctrl+C 退出后恢复数字人正常回复。"""
    import atexit
    import signal
    from . import owner_inbox
    if owner_inbox.watching():
        return _fail(args, '已经有一个 larkx watch 在运行')
    owner_inbox.start_watch()
    atexit.register(owner_inbox.stop_watch)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    since = _since_ts(args.since, time.time()) if args.since else None
    if not args.json:
        print('本人在线: 别人的消息数字人只判断、写收件箱, 不回复也不执行; Ctrl+C 退出后恢复。', flush=True)
    try:
        for e in owner_inbox.follow(since):
            print(json.dumps(e, ensure_ascii=False) if args.json else owner_inbox.format_entry(e), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        owner_inbox.stop_watch()
    return 0


def cmd_mentions(args):
    import time
    try:
        since = time.time() - state.parse_duration(args.since)
    except ValueError as e:
        return _fail(args, str(e))
    auth = LarkAuth()
    st = Storage()
    rows = st.mentions(auth.user_id, st.owner_name(auth.user_id), int(since))
    if args.json:
        _print_json(rows)
        return 0
    for r in rows:
        when = datetime.fromtimestamp(r['create_time']).strftime('%m-%d %H:%M') if r['create_time'] else ''
        where = '私聊' if r['kind'] == 'p2p' else f"「{r['chat_name'] or r['chat_id']}」{'回你' if r['kind'] == 'reply' else '@你'}"
        print(f"{when} {'  ' if r['replied'] else '● '}{where} {r['sender_name']}: {(r['content'] or '')[:80]}")
    if not rows:
        print('这段时间没有人私聊、@ 你或回你')
    return 0


def cmd_task(args):
    st = Storage()
    agent = is_agent_actor()
    turn = state.read_turn() if agent else None
    if args.action == 'list':
        rows = [_task_row(t) for t in st.list_tasks(include_finished=args.all)]
        if args.json:
            _print_json(rows)
            return 0
        for r in rows:
            every = f' 每{r["every_sec"]}s' if r['every_sec'] else ''
            print(f"#{r['id']} [{r['status']}] {r['due_at']}{every} chat={r['chat_id'] or '-'}: {r['prompt'][:80]}")
        return 0
    if args.action == 'cancel':
        if not args.text or not args.text[0].isdigit():
            return _fail(args, '用法: larkx task cancel <任务 id>')
        t = st.get_task(int(args.text[0]))
        if not t:
            return _fail(args, f'没有任务 #{args.text[0]}')
        if agent and not (turn and turn['is_owner']) and t.chat_id != (turn or {}).get('chat_id'):
            return _fail(args, '只能取消当前会话的任务; 其他任务需要本人发起')
        st.update_task(t.id, status='cancelled')
        if args.json:
            _print_json({'ok': True, 'id': t.id})
        else:
            print(f'已取消任务 #{t.id}')
        return 0
    # add
    prompt = _read_send_text(args)
    if not prompt.strip():
        return _fail(args, '任务内容为空')
    try:
        if args.at:
            due = state.parse_at(args.at)
        elif getattr(args, 'in_'):
            due = datetime.now().timestamp() + state.parse_duration(args.in_)
        else:
            return _fail(args, '需要 --at <时间> 或 --in <时长>')
        every = state.parse_duration(args.every) if args.every else 0
    except ValueError as e:
        return _fail(args, str(e))
    chat_id, anchor, by_owner, creator = args.chat or '', args.root or '', True, 'human'
    if agent:
        if not turn:
            return _fail(args, '当前没有正在处理的消息, 数字人不能新建任务')
        if turn['kind'] in ('system', 'report') and not turn['chat_id']:
            return _fail(args, '维护任务里找不到本人私聊, 不能安排提醒')
        # 维护任务、汇报里安排的提醒都落在本人私聊, 到点按访客规则执行, 只能在本人私聊提醒
        by_owner, creator = turn['is_owner'], ('system' if turn['kind'] in ('system', 'report') else 'agent')
        chat_id = chat_id or turn['chat_id']
        anchor = anchor or (turn['anchor'] if chat_id == turn['chat_id'] else '')
        if chat_id != turn['chat_id'] and not by_owner:
            return _fail(args, '只能给当前会话安排任务; 其他会话需要本人发起')
    task_id = st.add_task(kind='task', due_at=int(due), every_sec=every, prompt=prompt, chat_id=chat_id, anchor=anchor,
                          by_owner=1 if by_owner else 0, creator=creator)
    when = datetime.fromtimestamp(due).strftime('%Y-%m-%d %H:%M:%S')
    if args.json:
        _print_json({'ok': True, 'id': task_id, 'due_at': when, 'every_sec': every, 'chat_id': chat_id})
    else:
        print(f'已安排任务 #{task_id}: {when}' + (f', 之后每 {args.every} 一次' if every else ''))
    return 0


def cmd_say(args):
    import time
    text = _read_send_text(args)
    if not text.strip():
        return _fail(args, '内容为空')
    st = Storage()
    task_id = st.add_task(kind='say', due_at=0, prompt=text, by_owner=1, creator='human')
    if not args.wait:
        print(f'已交给数字人(#{task_id}), 结果: larkx task list --all')
        return 0
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        t = st.get_task(task_id)
        if t.status in ('done', 'failed'):
            if args.json:
                _print_json({'ok': t.status == 'done', 'id': t.id, 'result': t.result})
            else:
                print(t.result or '(数字人没有文字回答)')
            return 0 if t.status == 'done' else 1
        time.sleep(1)
    return _fail(args, f'等待超时, 任务 #{task_id} 仍在排队或处理中; daemon 是否在运行? larkx service status')


def _owner_id() -> str:
    """本人的 user_id(读本地凭证, 不联网); 没登录时为空。"""
    from .config import owner_user_id
    return owner_user_id()


def cmd_jobs(args):
    """例行任务: list 看每个任务的上次结果和下次时间; log 看运行记录; run 让 daemon 马上跑一次(数字人不能用)。"""
    from . import jobs
    from .daemon.prompts import BUILDERS
    st, cfg, now = Storage(), load_config(), datetime.now()
    store = jobs.JobStore(st)
    if args.action == 'log':
        rows = [jobs.describe(r) for r in store.log(args.job, args.limit)]
        if args.json:
            _print_json(rows)
            return 0
        for r in rows:
            detail = (r['error'] or r['summary']).replace('\n', ' ')
            print(f"#{r['id']} {r['job']} [{r['status_name']}] {r['created_at']} {r['trigger']}"
                  + (f" 第{r['attempt']}次" if r['attempt'] > 1 else '') + (f": {detail[:120]}" if detail else ''))
        return 0
    if args.action == 'run':
        if inside_twin():
            return _fail(args, '数字人不能手动触发例行任务', code=3)
        job = jobs.get(args.name or '')
        if not job:
            return _fail(args, f'用法: larkx jobs run <名字>, 可用: {", ".join(j.name for j in jobs.JOBS)}')
        if args.preview:
            if job.kind == 'local':
                return _fail(args, f'{job.name} 不是交给数字人的任务, 没有提示词')
            print(BUILDERS[job.name](cfg, st, now, _owner_id()))
            return 0
        cid = st.control_add(f'job:{job.name}')
        for _ in range(30):
            c = st.control_get(cid)
            if c and c.status != 'pending':
                return (_print_json({'ok': True, 'result': c.result}) or 0) if args.json else (print(c.result) or 0)
            time.sleep(0.5)
        return _fail(args, '已提交, 但 daemon 15 秒内没有接手(没在运行?), 用 larkx service status 查看')
    rows = []
    for job in jobs.JOBS:
        last = jobs.describe(store.last(job.name))
        nxt = jobs.next_time(job, cfg, now)
        rows.append({'name': job.name, 'title': job.title, 'kind': job.kind, 'enabled': job.when(cfg) is not None,
                     'next': nxt.strftime('%Y-%m-%d %H:%M') if nxt else '', 'last': last})
    if args.json:
        _print_json(rows)
        return 0
    for r in rows:
        last = r['last']
        state_text = (f"上次 {last['created_at']} {last['status_name']}" if last else '还没跑过')
        detail = ((last.get('error') or last.get('summary') or '').replace('\n', ' ')[:80]) if last else ''
        print(f"{r['name']:<12} {r['title']}  {'开' if r['enabled'] else '关'}  {state_text}"
              + (f"  下次 {r['next']}" if r['next'] else '') + (f"\n    {detail}" if detail else ''))
    return 0


def cmd_backfill(args):
    """回填历史消息(首次启动时 daemon 会自动做一次)。数字人不能用。"""
    from datetime import timedelta
    from . import backfill, groups, jobs
    st, cfg = Storage(), load_config()
    store = jobs.JobStore(st)
    days = args.days or int(cfg.get('backfill_days') or 30)
    running = store.last('backfill')
    if running and running.status == 'running' and running.started_at and \
            running.started_at > datetime.now() - timedelta(minutes=30):
        return _fail(args, 'daemon 正在回填, 进度用 larkx jobs 查看')
    client = LarkClient(LarkAuth())
    owner = str(client.auth.user_id)
    if args.dry_run:
        ids = args.chat or client.feed_chats(time.time() - days * 86400, max_pages=50)
        print(f'最近 {days} 天有更新的会话 {len(ids)} 个; 每个最多 {backfill.PER_CHAT} 条(测试/机器人类只拉 '
              f'{backfill.LOW_VALUE_DAYS} 天、最多 {backfill.LOW_VALUE_CAP} 条), 总量上限 {backfill.TOTAL_CAP} 条')
        return 0

    def show(s):
        print(f"\r  {s.get('chats_done', 0)}/{len(s.get('order') or [])} 个会话, 入库 {s.get('messages', 0)} 条", end='',
              flush=True, file=sys.stderr)
    if args.chat:
        ids = _resolve_chats(args, groups.ChatStore(st), args.chat)
        if ids is None:
            return 1
        res = backfill.Backfill(client, st, owner, days, progress=show).run(only=ids)
    else:
        saved = {} if args.again else store.get_state('backfill')
        if saved.get('done'):
            return _fail(args, '已经回填过了; 要重新回填用 --again, 只补某个会话用 --chat')

        def progress(s):
            if s.get('done') and not s.get('finished_at'):
                s['finished_at'] = time.time()
            store.put_state('backfill', s)
            show(s)
        run = store.create('backfill', f'manual-{int(time.time())}', 'manual')
        store.start(run.id)
        try:
            res = backfill.Backfill(client, st, owner, days, saved=saved, progress=progress).run()
        except Exception as e:
            store.fail(run.id, repr(e))
            raise
        store.finish(run.id, backfill.summary(res))
        if cfg.get('chat_groups'):
            prof = store.create('profile', 'after-backfill', 'manual')
            r = groups.profile_all(client, st, owner)
            store.finish(prof.id, f"{r['chats']} 个会话; 新会话 {len(r['new'])} 个")
    print(file=sys.stderr)
    if args.json:
        _print_json({'ok': True, **res})
    else:
        tail = '' if args.chat else ('; 会话画像已刷新' if cfg.get('chat_groups') else '') + \
            ('; daemon 接着会初始化记忆' if 0 <= cfg.get('memory_review_hour', -1) <= 23 else '')
        print(backfill.summary(res) + tail)
    return 0


def cmd_organize(args):
    """整理方案: list/show 查看; submit 提交(数字人在整理任务里用); apply/reject/undo 执行、放弃、撤销(本人)。
    要打开 LARKX_ORGANIZE(和会话分类); 没打开时一律拒绝。"""
    from . import notify, organize
    from .config import trusted_config
    cfg = trusted_config()  # 数字人进程里不信它自己带的 LARKX_ORGANIZE 等环境变量
    if not organize.enabled(cfg):
        return _fail(args, '整理方案没有打开: .env 里设 LARKX_CHAT_GROUPS=true 和 LARKX_ORGANIZE=true', code=3)
    st = Storage()
    store = organize.OrganizeStore(st, organize.enabled_kinds(cfg))
    agent = inside_twin()  # 不只看 LARKX_ACTOR: env -u LARKX_ACTOR 再调也认得出是数字人
    turn = state.read_turn() if agent else None
    owner_turn = not agent or bool(turn and turn.get('is_owner') and turn.get('kind') in ('message', 'say'))
    act = args.action
    if act == 'list':
        rows = [{'id': p.id, 'trigger': p.trigger, 'status': p.status, 'summary': p.summary,
                 'created_at': p.created_at.strftime('%Y-%m-%d %H:%M'),
                 'items': {k: sum(1 for i in store.items(p.id) if i.status == k)
                           for k in ('pending', 'applied', 'failed', 'rejected', 'undone')}} for p in store.list(args.limit)]
        if args.json:
            _print_json(rows)
            return 0
        for r in rows:
            counts = '、'.join(f"{organize_status(k)} {n}" for k, n in r['items'].items() if n)
            print(f"#{r['id']} {r['created_at']} {organize.TRIGGER_NAMES.get(r['trigger'], r['trigger'])} [{r['status']}] {counts}"
                  + (f": {r['summary']}" if r['summary'] else ''))
        return 0
    if act == 'submit':
        ref = (turn or {}).get('ref', '')
        if agent and not (owner_turn or (turn and turn.get('kind') == 'system' and ref.startswith('job:organize'))):
            return _fail(args, '只有整理会话的例行任务或本人发起时才能提交整理方案', code=3)
        if not args.target:
            return _fail(args, '用法: larkx organize submit <方案 JSON 文件, - 表示从 stdin 读>')
        try:
            text = sys.stdin.read() if args.target == '-' else open(args.target, encoding='utf-8').read()
            trigger = 'init' if ref.startswith('job:organize_init') else 'weekly' if ref.startswith('job:organize#') else 'manual'
            p = store.submit(json.loads(text), trigger=trigger, source_ref=ref)
        except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            return _fail(args, f'方案不合格: {e!r}')
        _audit(st, 'organize', '', f'提交整理方案 #{p.id}')
        if args.json:
            _print_json({'ok': True, 'id': p.id, 'text': store.render(p.id)})
        else:
            print(store.render(p.id))
        return 0
    if act == 'propose':
        if not owner_turn:
            return _fail(args, '只有本人发起时才能要整理方案', code=3)
        cid = st.control_add('job:organize')
        return (print(f'已请 daemon 生成整理方案(控制请求 #{cid}), 写好后会私信给你') or 0)
    target = str(args.target or '').lstrip('#')
    if act in ('apply', 'reject', 'undo') and not target.isdigit():
        return _fail(args, f'要写明方案编号, 如 larkx organize {act} 3' + (' --items 1-5,8' if act != 'undo' else ''))
    prop = store.get(int(target)) if target.isdigit() else store.get()
    if not prop:
        return _fail(args, '还没有整理方案' if not args.target else f'没有整理方案 #{args.target}')
    if act == 'show':
        if args.json:
            _print_json({'id': prop.id, 'status': prop.status, 'text': store.render(prop.id),
                         'items': [{'seq': i.seq, 'kind': i.kind, 'title': i.title, 'reason': i.reason, 'status': i.status,
                                    'error': i.error} for i in store.items(prop.id)]})
        else:
            print(store.render(prop.id))
        return 0
    if not owner_turn:
        return _fail(args, '整理方案只有本人同意后才能执行或撤销', code=3)
    if agent:
        from . import groups
        from .storage.models import Chat, ChatProfile
        s = st.Session()
        try:
            cid = str(turn.get('chat_id') or '')
            profiles = {p.chat_id: p for p in s.query(ChatProfile).filter(ChatProfile.chat_id == cid)}
            chat = s.get(Chat, cid) if cid else None
        finally:
            s.close()
        # 和清理、"不清理"标记用同一个判断: 记下的本人私聊(必须是私聊), 或者私聊对象是本人
        if not cid or (chat and chat.chat_type not in (0, 1)) or cid not in groups.own_chats(profiles, cfg, _owner_id()):
            return _fail(args, '整理方案只能在本人私聊里执行或撤销', code=3)
    try:
        if act == 'undo':
            res = store.undo(prop.id, organize.FeishuOps(LarkClient(LarkAuth())))
        else:
            seqs = organize.parse_items(args.items, len(store.items(prop.id)))
            if act == 'reject':
                n = store.reject(prop.id, seqs)
                _audit(st, 'organize', '', f'放弃整理方案 #{prop.id} 第 {args.items} 条')
                return (print(f'已放弃 {n} 条') or 0)
            res = store.apply(prop.id, seqs, organize.FeishuOps(LarkClient(LarkAuth())))
    except ValueError as e:
        return _fail(args, str(e))
    _audit(st, 'organize', '', f'{"撤销" if act == "undo" else "执行"}整理方案 #{prop.id}: {res}')
    if args.json:
        _print_json({'ok': True, 'id': prop.id, 'results': [{'seq': s, 'result': r} for s, r in res]})
    else:
        for s, r in res:
            print(f'{s}. {r}')
        print(f'没有需要{"撤销" if act == "undo" else "执行"}的条目' if not res else
              (f'撤销用: larkx organize undo {prop.id}' if act == 'apply' else ''))
    return 0


def organize_status(k):
    return {'pending': '待定', 'applied': '已执行', 'failed': '失败', 'rejected': '已放弃', 'undone': '已撤销'}.get(k, k)


def cmd_memory(args):
    """长期记忆: list 按字段筛选, lint 检查, stats 统计, index 重建索引。数字人也能用。"""
    from pathlib import Path
    from . import memory
    cfg = load_config()
    home = Path(cfg['agent_home'])
    if args.action == 'index':
        if not cfg.get('memory_structured'):
            return _fail(args, '没有打开结构化记忆(LARKX_MEMORY_STRUCTURED), MEMORY.md 还是手写的, 不自动生成')
        changed = memory.build_index(home)
        return (print('索引已更新' if changed else '索引已是最新') or 0)
    if args.action == 'stats':
        s = memory.stats(home)
        if args.json:
            _print_json(s)
        else:
            print(f"{s['files']} 个记忆文件(" + '、'.join(f'{k} {v}' for k, v in s['by_type'].items()) +
                  f"), 归档 {s['archived']} 个, 待跟进 {s['todo']} 件, {s['bytes'] // 1000}KB; "
                  f"问题: {s['errors']} 个错误、{s['warnings']} 个提醒")
        return 0
    if args.action == 'lint':
        issues = memory.lint(home)
        if args.json:
            _print_json([{'level': l, 'file': f, 'message': m} for l, f, m in issues])
        else:
            for l, f, m in issues:
                print(f"{'❌' if l == 'error' else '⚠️ ' if l == 'warn' else 'ℹ️ '} {f}: {m}")
            print('没有问题' if not issues else f'共 {len(issues)} 个问题')
        return 1 if any(l == 'error' for l, _, _ in issues) else 0
    try:
        due = state.parse_duration(args.due) // 86400 if args.due else None
    except ValueError as e:
        return _fail(args, str(e))
    rows = memory.select(home, args.type, args.status, due, args.about)
    if args.json:
        _print_json([{'file': e['file'], **e['meta']} for e in rows])
        return 0
    for e in rows:
        m = e['meta']
        extra = ' '.join(x for x in (m.get('status', ''), f"截止 {m['due']}" if m.get('due') else '',
                                     f"更新 {m['updated']}" if m.get('updated') else '') if x)
        print(f"[{m.get('type', '?')}] {m.get('name') or e['file']} ({e['file']}) {extra}")
    return 0


def cmd_prune(args):
    """清理旧数据: 不带 --days 时按关注度和保留期清理消息、整理旧记录; 带 --days 时只清 --chat 指定的会话。数字人不能用。"""
    from . import groups, jobs, retention
    if inside_twin():  # 不只靠 LARKX_ACTOR 和 Claude 的命令拦截: Codex 后端没有那一层
        return _fail(args, '数字人不能清理消息', code=3)
    st = Storage()
    if args.days is None:
        if args.chat:
            return _fail(args, '--chat 要和 --days 一起用')
        store = jobs.JobStore(st)
        vac = store.get_state('prune')
        res = retention.run(load_config(), st, dry_run=args.dry_run, state=vac, owner_id=_owner_id())
        if not args.dry_run:
            store.put_state('prune', vac)
        text = retention.summary(res, args.dry_run)
    else:
        if args.days <= 0:
            return _fail(args, '--days 必须大于 0')
        if not args.chat:
            return _fail(args, '指定 --days 时要用 --chat <会话名或 chat_id> 说明清哪个会话; 不指定时按关注度清理')
        ids = _resolve_chats(args, groups.ChatStore(st), args.chat)
        if ids is None:
            return 1
        n = st.prune_messages(time.time() - args.days * retention.DAY, ids, dry_run=args.dry_run)
        res, text = {'total': n}, f"{len(ids)} 个会话 {args.days} 天前的消息: {'会删' if args.dry_run else '删'} {n} 条"
    if args.json:
        _print_json({'ok': True, 'dry_run': args.dry_run, **{k: v for k, v in res.items()}, 'summary': text})
    else:
        print(text)
    return 0


def cmd_audit(args):
    rows = Storage().list_actions(limit=args.limit)
    if args.json:
        _print_json([{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows])
        return 0
    for r in rows:
        mark = '✓' if r.ok else '✗'
        turn = f' ({r.turn_kind} {r.turn_ref})' if r.turn_kind else ''
        detail = (r.content or '').replace('\n', ' ')[:60] + (f' | {r.error}' if r.error else '')
        print(f'{r.created_at:%m-%d %H:%M:%S} {mark} {r.actor}{turn} {r.action} chat={r.chat_id}: {detail}')
    return 0


TWIN_WHITELIST_DENIED = '数字人不能修改唤起白名单, 只有本人能改; 想加进白名单的人请直接找本人'


def _choose(prompt: str, labels: list):
    """在终端里从候选中选一个, 返回下标; 取消时返回 None。"""
    try:
        import questionary
        ans = questionary.select(prompt, choices=[questionary.Choice(l, value=i) for i, l in enumerate(labels)]
                                 + [questionary.Choice('取消', value=-1)]).ask()
        return None if ans is None or ans < 0 else ans
    except Exception:
        pass
    print(prompt)
    for i, l in enumerate(labels, 1):
        print(f'  [{i}] {l}')
    c = input('选择编号(回车取消): ').strip()
    return int(c) - 1 if c.isdigit() and 1 <= int(c) <= len(labels) else None


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _pick_person(args, people: list, what: str, rerun: str):
    """多个候选时让本人选: 终端里交互选择; 不是终端(或 --json)时列出候选, 请本人用 user_id 重新运行。"""
    from .whitelist import label
    if _interactive() and not args.json:
        i = _choose(f'{what}, 选一个:', [label(p) for p in people])
        if i is None:
            print('已取消')
        return people[i] if i is not None else None
    msg = f'{what}, 用 user_id 重新运行: larkx whitelist {rerun} <user_id>'
    if args.json:
        _print_json({'ok': False, 'error': msg, 'candidates': people})
    else:
        print(msg, file=sys.stderr)
        for p in people:
            print(f'  {label(p)}', file=sys.stderr)
    return None


def _owner_name(st) -> str:
    """本人的名字(从本地消息记录里取, 不联网); 拿不到时返回空字符串。"""
    try:
        uid = LarkAuth().user_id
    except Exception:
        return ''
    return st.owner_name(uid) if uid else ''


def cmd_whitelist(args):
    from . import whitelist as wl
    from .config import inside_twin
    st = Storage()
    cfg = load_config()
    triggers = cfg['triggers']
    query = ' '.join(args.target).strip()
    if args.action == 'list':
        rows = st.whitelist_list()
        rule = wl.rule_sentence(len(rows), triggers, below=False)
        deny = wl.deny_sentence(cfg, len(rows), _owner_name(st) if '{owner}' in (cfg.get('deny_reply') or '') else '')
        if args.json:
            _print_json({'rule': rule, 'deny': deny, 'owner_only': bool(triggers.get('owner_only')),
                         'group_at_only': bool(triggers.get('group_at_only')), 'entries': [wl.entry_dict(r) for r in rows]})
            return 0
        print(rule)
        if deny:
            print(deny)
        for r in rows:
            print(f'{r.user_id}\t{r.name or "-"}\t{r.department or "-"}\t添加于 {r.added_at:%Y-%m-%d %H:%M}')
        return 0
    if inside_twin():
        # 数字人的会话里(包括清掉 LARKX_ACTOR 之后): 不能改, 免得有人让它把自己加进去
        turn = state.read_turn()
        st.log_action(actor='agent', action='whitelist', content=f'{args.action} {query}'[:300], ok=0,
                      error=TWIN_WHITELIST_DENIED, turn_kind=(turn or {}).get('kind', ''), turn_ref=(turn or {}).get('ref', ''))
        try:
            from .notify import Notifier
            verb = '加进' if args.action == 'add' else '移出'
            Notifier(load_config(), LarkClient(LarkAuth())).alert(
                f'数字人想把「{query}」{verb}唤起白名单, 已拦截。白名单只能由你在控制台或终端(larkx whitelist)里改',
                key=f'whitelist:{args.action}:{query}', cooldown=600)
        except Exception as e:
            logger.debug(f'白名单拦截告警没发出去: {e!r}')
        return _fail(args, TWIN_WHITELIST_DENIED, code=3)
    if not query:
        return _fail(args, f'用法: larkx whitelist {args.action} <名字或 user_id>')
    if args.action == 'add':
        client = LarkClient(LarkAuth())
        if wl.is_user_id(query):
            person = wl.lookup_user(client, query)
            if not person:
                return _fail(args, f'找不到 user_id 为 {query} 的人')
        else:
            people = wl.search_people(client, query)
            if not people:
                return _fail(args, f'没有搜到叫「{query}」的人')
            # 只有一个候选并且名字完全一样时直接加; 否则(同名、或者只是名字相近)让本人确认是哪一个
            if len(people) == 1 and people[0]['name'] == query:
                person = people[0]
            else:
                person = _pick_person(args, people, f'搜到 {len(people)} 个人' if len(people) > 1 else f'只搜到名字相近的「{people[0]["name"]}」',
                                      'add')
                if not person:
                    return 1
        if person['user_id'] == str(client.auth.user_id):
            return _fail(args, '你本人一直能唤起数字人, 不用加进白名单')
        created = st.whitelist_add(person['user_id'], person['name'], person['department'], person['avatar_key'], added_by='cli')
        if created:
            _audit(st, 'whitelist', '', f'加入白名单 {wl.label(person)}')
        msg = ('已加入白名单: ' if created else '已经在白名单里: ') + wl.label(person)
        changed = person
    else:
        rows = st.whitelist_list()
        if wl.is_user_id(query):
            matches = [r for r in rows if r.user_id == query]
        else:
            matches = [r for r in rows if r.name == query] or [r for r in rows if query in (r.name or '')]
        if not matches:
            return _fail(args, f'白名单里没有「{query}」; 用 larkx whitelist list 查看')
        if len(matches) > 1:
            picked = _pick_person(args, [wl.entry_dict(r) for r in matches], f'白名单里有 {len(matches)} 个「{query}」', 'remove')
            if not picked:
                return 1
            matches = [r for r in matches if r.user_id == picked['user_id']]
        row = matches[0]
        st.whitelist_remove(row.user_id)
        _audit(st, 'whitelist', '', f'移出白名单 {wl.label(row)}')
        msg, changed = '已移出白名单: ' + wl.label(row), wl.entry_dict(row)
    rule = wl.rule_sentence(len(st.whitelist_ids()), triggers, below=False)
    if args.json:
        _print_json({'ok': True, 'person': changed, 'rule': rule})
    else:
        print(f'{msg}\n现在{rule}。立即生效, 不用重启。')
    return 0


def cmd_skill(args):
    from .skills import install_user_skill
    targets = ['claude', 'codex'] if args.target == 'all' else [args.target]
    for t in targets:
        path = install_user_skill(t)
        print(f'已安装 larkx skill 到 {t}: {path}')
    return 0


def cmd_service(args):
    import subprocess
    from . import service
    if not service.available():
        return _fail(args, '本机没有 systemd, 无法托管为服务; 可以直接运行 larkx daemon')
    kinds = service.kinds(args.unit)
    if args.action == 'install':
        for path in service.install(args.unit):
            print(f'已安装并设为开机自启: {path}')
        print('启动: larkx service start')
        return 0
    if args.action == 'uninstall':
        service.uninstall(args.unit)
        print('已停止并卸载服务')
        return 0
    if args.action in ('start', 'stop', 'restart'):
        for kind in kinds:
            if not service.unit_path(kind).exists():
                return _fail(args, f'{service.UNITS[kind]} 未安装, 先运行 larkx service install')
            r = service.systemctl(args.action, service.UNITS[kind])
            if r.returncode != 0:
                return _fail(args, f'systemctl {args.action} {service.UNITS[kind]} 失败: {r.stderr.strip()}')
    if args.action == 'logs':
        from .daemon import LOG_PATH
        if not LOG_PATH.exists():
            return _fail(args, f'还没有日志文件 {LOG_PATH}, 服务可能从未成功启动; 可以试试 journalctl --user -u {service.UNIT}')
        cmd = ['tail', '-n', str(args.lines)] + (['-F'] if args.follow else []) + [str(LOG_PATH)]
        return subprocess.run(cmd).returncode
    info = {kind: service.status(kind) for kind in kinds}
    stray = service.stray_daemons()
    if args.json:
        _print_json({'units': info, 'stray_daemons': stray})
        return 0
    for kind, st in info.items():
        label = '运行中' if st['active'] else ('已停止' if st['installed'] else '未安装')
        print(f"{st['unit']}: {label}  pid={st['main_pid'] or '-'}  开机自启={st['enabled']}")
    if stray:
        print(f"注意: 还有不归 systemd 管的 larkx daemon 进程 {stray}, 同一账号会出现两个数字人")
    return 0


def cmd_web(args):
    from .web.app import get_token, login_url, run_web
    cfg = load_config()
    if args.action == 'url':
        print(login_url(cfg))
        return 0
    if args.action == 'reset-token':
        get_token(reset=True)
        print('令牌已重置, 之前登录的浏览器都需要重新打开登录链接:\n' + login_url(cfg))
        return 0
    run_web()
    return 0


def cmd_doctor(args):
    from . import doctor
    checks = doctor.run(skip_model=args.skip_model)
    failed = any(c['level'] == doctor.FAIL for c in checks)
    if args.json:
        _print_json({'ok': not failed, 'checks': checks})
    else:
        mark = {doctor.OK: '✅', doctor.WARN: '⚠️ ', doctor.FAIL: '❌'}
        for c in checks:
            print(f"{mark[c['level']]} {c['name']}: {c['detail']}")
            if c['fix'] and c['level'] != doctor.OK:
                print(f"    → {c['fix']}")
    return 1 if failed else 0


def _plugin_flags():
    """全局的 --memory X / --comms Y(写在子命令前后都行): 这一次运行临时切换插件, 等同于设 LARKX_MEMORY / LARKX_COMMS。
    在 argparse 之前处理, 子命令(包括 daemon)读到的配置就是切换后的。"""
    argv, out = sys.argv[1:], []
    i = 0
    while i < len(argv):
        a = argv[i]
        key = next((k for k in ('memory', 'comms') if a == f'--{k}' or a.startswith(f'--{k}=')), None)
        # larkx plugin use --memory X 是它自己的参数, 不在这里吃掉
        if key and 'plugin' not in out:
            value = a.split('=', 1)[1] if '=' in a else (argv[i + 1] if i + 1 < len(argv) else '')
            i += 1 if '=' in a else 2
            if not value:
                sys.exit(f'--{key} 要写名字, 可选: {", ".join(plugins.names(key))}')
            if value not in plugins.names(key):
                sys.exit(f'未知的{"记忆策略" if key == "memory" else "交互协议"} {value}, 可选: {", ".join(plugins.names(key))}')
            os.environ[plugins.ENV[key]] = value
            continue
        out.append(a)
        i += 1
    sys.argv[1:] = out


def _env_file():
    from .service import PROJECT_DIR
    return PROJECT_DIR / '.env'


def _write_env(path, values: dict):
    """改 .env 里的这几项(已有就替换那一行, 没有就追加), 其他内容不动; 值为 None 时删掉那一行。"""
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
        if key in values:
            seen.add(key)
            if values[key] is not None:
                out.append(f'{key}={values[key]}')
            continue
        out.append(line)
    for key, value in values.items():
        if key not in seen and value is not None:
            out.append(f'{key}={value}')
    path.write_text('\n'.join(out) + '\n', encoding='utf-8')


def cmd_plugin(args):
    """插件: 看可选的记忆策略和交互协议, 或者写进 .env 长期切换(重启 daemon 后生效)。"""
    if args.action == 'use':
        if not args.use_memory and not args.use_comms:
            return _fail(args, '用法: larkx plugin use --memory <名字> [--comms <名字>]')
        values = {}
        for kind, value in (('memory', args.use_memory), ('comms', args.use_comms)):
            if value and value not in plugins.names(kind):
                return _fail(args, f'未知的{"记忆策略" if kind == "memory" else "交互协议"} {value}, 可选: {", ".join(plugins.names(kind))}')
        if args.use_memory:
            values['LARKX_MEMORY'] = args.use_memory
            # 只换记忆策略时, 交互协议回到"跟着记忆策略走"
            values['LARKX_COMMS'] = args.use_comms
        elif args.use_comms:
            values['LARKX_COMMS'] = args.use_comms
        path = _env_file()
        _write_env(path, values)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cfg = load_config()
        msg = f'已写进 {path}: {plugins.describe(cfg)}。重启 daemon 生效: larkx service restart'
        _print_json({'ok': True, 'memory': cfg['memory'], 'comms': cfg['comms'], 'env': str(path)}) if args.json else print(msg)
        return 0
    cfg = load_config()
    out = {}
    for kind in plugins.KINDS:
        out[kind] = [{'name': n, 'active': cfg.get(kind) == n, 'description': plugins.plugin_class(kind, n).description}
                     for n in plugins.names(kind)]
    if args.json:
        _print_json(out)
        return 0
    for kind, title in (('memory', '记忆策略(--memory)'), ('comms', '交互协议(--comms)')):
        print(title)
        for item in out[kind]:
            print(f"  {'*' if item['active'] else ' '} {item['name']:<10} {item['description']}")
    print(f'当前: {plugins.describe(cfg)}。--comms 不写时跟着记忆策略配套的协议走。')
    return 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    logger.remove()
    logger.add(sys.stderr, level='INFO')
    _plugin_flags()
    parser = argparse.ArgumentParser(prog='larkx', description='以本人身份操作飞书 / Lark 的 CLI, 也是数字人的工具',
                                     epilog='插件: larkx --memory <记忆策略> [--comms <交互协议>] <子命令> 这一次临时切换, '
                                            '如 larkx --memory work daemon; 长期切换用 larkx plugin use。')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('auth', help='凭证管理')
    p.add_argument('action', choices=['status', 'import', 'check', 'qr'])
    p.add_argument('--cookie', help='直接传 cookie 串')
    p.add_argument('--file', help='从文件读 cookie')
    p.add_argument('--region', choices=list(REGIONS), help='qr/import 登录的区域: feishu=飞书(国内版) lark=Lark(国际版);默认沿用已有凭证,无凭证时取 LARKX_REGION')
    p.set_defaults(fn=cmd_auth)
    p = sub.add_parser('send', help='发送文本、markdown 富文本消息, 或者 --file 发图片和文件')
    p.add_argument('chat_id')
    p.add_argument('text', nargs='*', help='消息内容; 省略或写 - 时从 stdin 读取')
    p.add_argument('--root', help='回复哪条消息: 普通群和私聊里是主会话里的引用回复(那条已在话题里时回进话题), 话题群里回进话题')
    p.add_argument('--thread', action='store_true',
                   help='和 --root 一起用: 普通群里回进以那条为根的话题(没有话题就新开一个), 不在主会话里引用回复')
    p.add_argument('-m', '--markdown', action='store_true',
                   help='按 markdown 富文本发送: 代码块(```)、有序/无序列表、标题、引用、**加粗**、*斜体*、链接; 不带此选项时按纯文本发送')
    p.add_argument('--urgent', action='store_true', help='发出后立即加急(应用内); 私聊默认加急对方, 群里要配 --urgent-to')
    p.add_argument('--urgent-to', action='append', default=[], help='加急谁: user_id 或名字, 可重复; 带上它就会加急')
    p.add_argument('--urgent-type', choices=['app', 'sms', 'phone'],
                   help='加急类型, 默认 app(应用内); sms 短信、phone 电话可能计费, 要显式指定; 带上它就会加急')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_send)
    p.add_argument('--mention', action='append', default=[], metavar='USER_ID:NAME',
                   help='真实 @对象，可重复; 可以和 -m 一起用。正文里写了 "@显示名" 的就地变成 @, 没写到的放在正文最前面')
    p.add_argument('--file', action='append', default=[], metavar='PATH',
                   help='发送本地文件, 可重复: png/jpg/jpeg/gif/webp(不超过 10MB)按图片发, 其余按文件发; 带文字时先发文字再发文件')
    p = sub.add_parser('doc', help='云文档: read 读正文, comment 看评论上下文, reply 在评论下回复(要装好 lark-cli 并登录)')
    p.add_argument('action', choices=['read', 'comment', 'reply'])
    p.add_argument('chat_id', help='云文档会话 id(doc:<feed id>, 见触发消息或 larkx chats)')
    p.add_argument('comment_id', nargs='?', default='', help='评论 ID(触发消息里的 anchor); comment、reply 要给')
    p.add_argument('text', nargs='*', help='回复内容(纯文本); 省略或写 - 时从 stdin 读取')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_doc)
    p = sub.add_parser('forward', help='转发消息: 逐条转发到一个或几个会话, 或者 --merge 合并成一条聊天记录转发')
    p.add_argument('msg_id', nargs='+', help='要转发的消息 id(larkx messages / history 里的 msg_id), 可以写几个, 按顺序转发')
    p.add_argument('--to', action='append', required=True, metavar='CHAT_ID', help='转发到哪个会话, 可重复')
    p.add_argument('--merge', action='store_true',
                   help='合并转发: 几条消息合成一条"聊天记录"发出, 只能是同一个会话里的消息; 标题由飞书按源会话生成, 不能自定义')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_forward)
    p = sub.add_parser('config', help='查看生效中的配置(配置项写在 .env 里)')
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser('chats', help='列出本地会话, 带分类和关注度')
    p.add_argument('--attention', help='只看这些关注度, 逗号分隔: 重点关注,简单关注,只存不看')
    p.add_argument('--group', help='只看这个分类(含建议归到这个分类的)')
    p.add_argument('--active', help='最近多久有消息, 如 24h、7d')
    p.add_argument('--feishu-muted', action='store_true', help='只看在飞书里设了免打扰的')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_chats)
    p = sub.add_parser('groups', help='会话分类与关注度: 看、建、改、删分类, 把会话归进分类, 刷新画像')
    p.add_argument('action', nargs='?', choices=['list', 'add', 'edit', 'remove', 'assign', 'profile'], default='list')
    p.add_argument('targets', nargs='*', help='add/edit/remove: 分类名; assign: 会话名或 chat_id(可多个)')
    p.add_argument('--to', help='assign: 归到哪个分类(空字符串 = 移出分类)')
    p.add_argument('--attention', help='关注度: 重点关注 / 简单关注 / 只存不看; assign 时写「跟分类」恢复跟分类')
    p.add_argument('--keywords', help='add/edit: 分类关键词, 逗号分隔; 简单关注时这些词出现也算和你有关')
    p.add_argument('--name', help='edit: 新分类名')
    p.add_argument('--private', action=argparse.BooleanOptionalAction, default=None, help='私人分类: 内容不进工作记忆')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_groups)
    p = sub.add_parser('sessions', help='列出 agent 会话(session 表)')
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(fn=cmd_sessions)
    p = sub.add_parser('messages', help='读本地消息记录')
    p.add_argument('chat_id')
    p.add_argument('--anchor', default=None, help='话题 id;空字符串=仅主会话流')
    p.add_argument('--since', help='只看最近多久的, 如 24h、7d')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_messages)
    p = sub.add_parser('search', help='搜索用户/群')
    p.add_argument('query', nargs='+')
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser('api', help='调任意网关接口(类型名见 docs/cmd_table.md)')
    p.add_argument('type', help='proto 请求类型名(如 chats.PullChatsByIdsRequest)或 cmd 数字')
    p.add_argument('json_body', nargs='?', help='请求字段 JSON')
    p.set_defaults(fn=cmd_api)
    p = sub.add_parser('history', help='拉取会话历史消息')
    p.add_argument('chat_id')
    p.add_argument('--start', type=int, help='起始 position(默认从最新往前)')
    p.add_argument('--count', type=int, default=20)
    p.add_argument('--save', action='store_true', help='同时写入本地库')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser('download', help='下载消息里的图片/文件等资源')
    p.add_argument('msg_id', nargs='?', help='消息 id(从本地库查资源 key)')
    p.add_argument('--key', help='直接指定资源 key(img_v3_*/file_v3_*),需配合 --chat')
    p.add_argument('--chat', help='chat_id(配合 --key 或加速本地查找)')
    p.add_argument('-o', '--out', required=True, help='保存路径')
    p.set_defaults(fn=cmd_download)
    p = sub.add_parser('listen', help='常驻接收消息, 只入库不接数字人')
    p.set_defaults(fn=cmd_listen)
    p = sub.add_parser('daemon', help='常驻运行数字人: 收消息并交给一直开着的 agent 会话')
    p.set_defaults(fn=cmd_daemon)
    p = sub.add_parser('service', help='用 systemd 用户服务托管 daemon')
    p.add_argument('action', choices=['install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs'])
    p.add_argument('--unit', choices=['all', 'daemon', 'web'], default='all', help='操作哪个服务, 默认两个都操作')
    p.add_argument('-n', '--lines', type=int, default=100, help='logs 显示的行数')
    p.add_argument('-f', '--follow', action='store_true', help='logs 持续输出')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_service)
    p = sub.add_parser('recall', help='撤回一条自己发出的消息')
    p.add_argument('msg_id')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_recall)
    p = sub.add_parser('urgent', help='加急本人发出的一条消息(应用内/短信/电话); urgent status <msg_id> 看谁确认了')
    p.add_argument('target', nargs='+', help='<msg_id>; 或 status <msg_id> ...')
    p.add_argument('--to', action='append', default=[], help='加急谁: user_id 或名字, 可重复; 私聊默认对方, 群里必填')
    p.add_argument('--type', choices=['app', 'sms', 'phone'], default='app',
                   help='app 应用内(默认); sms 短信、phone 电话可能计费, 要显式指定, 数字人不能用')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_urgent)
    p = sub.add_parser('takeover', help='接管会话: 一段时间内对方的消息都交给数字人回复')
    p.add_argument('action', choices=['start', 'stop', 'list'])
    p.add_argument('chat_id', nargs='?')
    p.add_argument('--for', dest='for_', help='接管多久, 如 30m、2h; 默认 LARKX_TAKEOVER_DEFAULT')
    p.add_argument('--goal', help='交代给数字人的目标, 如"帮我和他约下周二开会"')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_takeover)
    p = sub.add_parser('whitelist', help='唤起白名单: 除了你还有谁能叫起数字人(非空时只有你和名单里的人能叫起); 数字人自己不能改')
    p.add_argument('action', choices=['list', 'add', 'remove'])
    p.add_argument('target', nargs='*', help='add: 名字(搜人, 同名时让你选)或 user_id; remove: 名单里的名字或 user_id')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_whitelist)
    p = sub.add_parser('rule', help='监听任务：消息匹配规则后，交给现有 agent 执行预设任务')
    p.add_argument('action', choices=['add','list','enable','pause','delete','runs'])
    p.add_argument('id', type=int, nargs='?')
    p.add_argument('--name')
    p.add_argument('--chat', help='来源群真实 chat_id')
    p.add_argument('--instruction', help='预设任务指令；- 从 stdin 读')
    p.add_argument('--sender', action='append', default=[], help='发送人 user_id，可重复；默认他人')
    p.add_argument('--mention', action='append', default=[], help='被 @的 user_id，任意命中，可重复')
    p.add_argument('--keyword', action='append', default=[], help='正文关键词，任意命中，可重复')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_rule)
    p = sub.add_parser('ping', help='通知本人: 这件事要本人亲自处理(数字人用; 记进本人收件箱 larkx inbox)')
    p.add_argument('text', nargs='*', help='要本人做什么, 一句话; 省略或写 - 时从 stdin 读')
    p.add_argument('--chat', default='', help='相关会话, 默认当前这一轮的会话')
    p.add_argument('--msg', default='', help='相关消息 id, 默认触发这一轮的消息')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_ping)
    p = sub.add_parser('inbox', help='本人收件箱: 数字人判断要你处理的事、别人 @ 你的消息')
    p.add_argument('--since', default='24h', help='多久以内, 如 2h、1d')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_inbox)
    p = sub.add_parser('watch', help='本人在线: 实时看收件箱; 运行期间数字人对别人的消息只判断、写收件箱, 不回复不执行')
    p.add_argument('--since', default='', help='先补看多久以内的旧条目, 如 1h; 默认只看新的')
    p.add_argument('--json', action='store_true', help='每条一行 JSON')
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser('mentions', help='最近谁找过你: 别人的私聊、@你的群消息、回你消息或在你话题里的消息, 以及你之后回没回')
    p.add_argument('--since', default='24h', help='多久以内, 如 12h、1d')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_mentions)
    p = sub.add_parser('task', help='定时任务: 到点后交给数字人处理')
    p.add_argument('action', choices=['add', 'list', 'cancel'])
    p.add_argument('text', nargs='*', help='add: 任务内容(省略或写 - 时从 stdin 读); cancel: 任务 id')
    p.add_argument('--at', help='时间, 如 "2026-10-22 09:00"、"10-22 09:00"、"09:00"')
    p.add_argument('--in', dest='in_', help='多久之后, 如 30m、2h、1d')
    p.add_argument('--every', help='周期, 如 1d、1w; 不填则只执行一次')
    p.add_argument('--chat', help='关联的会话 id(到点时在这里通知)')
    p.add_argument('--root', help='关联的话题 id')
    p.add_argument('--all', action='store_true', help='list: 包括已完成和已取消的')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_task)
    p = sub.add_parser('say', help='以本人身份从终端给数字人下指令')
    p.add_argument('text', nargs='*', help='指令内容; 省略或写 - 时从 stdin 读')
    p.add_argument('--wait', action='store_true', help='等数字人处理完并打印它的回答')
    p.add_argument('--timeout', type=int, default=600)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_say)
    p = sub.add_parser('jobs', help='例行任务(每日回顾、每周整理等): 看状态、看运行记录、手动运行')
    p.add_argument('action', nargs='?', choices=['list', 'log', 'run'], default='list')
    p.add_argument('name', nargs='?', help='run: 任务名')
    p.add_argument('--job', help='log: 只看这个任务')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--preview', action='store_true', help='run: 只打印交给数字人的提示词, 不运行')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_jobs)
    p = sub.add_parser('backfill', help='回填历史消息(首次启动时会自动做一次); 数字人不能用')
    p.add_argument('--days', type=int, help='回填最近多少天, 默认 LARKX_BACKFILL_DAYS')
    p.add_argument('--chat', action='append', default=[], help='只回填这个会话(名字或 chat_id), 可重复')
    p.add_argument('--again', action='store_true', help='已经回填过也重新回填一遍')
    p.add_argument('--dry-run', action='store_true', help='只看会回填多少会话')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_backfill)
    p = sub.add_parser('organize', help='整理方案(分类、飞书标签、置顶、标记、免打扰): 看、提交、执行、放弃、撤销')
    p.add_argument('action', nargs='?', choices=['list', 'show', 'submit', 'propose', 'apply', 'reject', 'undo'],
                   default='show')
    p.add_argument('target', nargs='?', help='apply/reject/undo: 方案编号(必填); show: 方案编号(默认最新); submit: JSON 文件或 -')
    p.add_argument('--items', default='', help='apply/reject: 第几条(必填), 如 1-5,8; 全部写 全部')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_organize)
    p = sub.add_parser('memory', help='长期记忆: 按字段筛选、检查、统计、重建索引')
    p.add_argument('action', nargs='?', choices=['list', 'lint', 'stats', 'index'], default='list')
    p.add_argument('--type', choices=['我', '人', '群', '事'])
    p.add_argument('--status', choices=['进行中', '待跟进', '已完成'])
    p.add_argument('--due', help='截止日期在多久以内(含已过期), 如 7d')
    p.add_argument('--about', help='和某个人或群有关(名字)')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_memory)
    p = sub.add_parser('prune', help='清理旧数据: 默认按关注度和保留期, 也可以 --days N --chat 会话 单独清; 数字人不能用')
    p.add_argument('--days', type=int, help='只保留最近多少天(要配合 --chat)')
    p.add_argument('--chat', action='append', default=[], help='会话名或 chat_id, 可重复')
    p.add_argument('--dry-run', action='store_true', help='只统计会删多少, 不删')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_prune)
    p = sub.add_parser('audit', help='查看通过 larkx 对外做过的动作(发消息等)')
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_audit)
    p = sub.add_parser('skill', help='把 larkx skill 装进本机的 Claude Code / Codex, 让它们能操作你的飞书')
    p.add_argument('action', choices=['install'])
    p.add_argument('--target', choices=['claude', 'codex', 'all'], default='all')
    p.set_defaults(fn=cmd_skill)
    p = sub.add_parser('web', help='网页控制台: 默认启动服务; url 打印登录链接; reset-token 重置令牌')
    p.add_argument('action', nargs='?', choices=['serve', 'url', 'reset-token'], default='serve')
    p.set_defaults(fn=cmd_web)
    p = sub.add_parser('doctor', help='检查登录、配置、模型调用和服务状态')
    p.add_argument('--skip-model', action='store_true', help='跳过模型调用检查')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser('plugin', help='插件: list 看可选的记忆策略和交互协议; use --memory X [--comms Y] 写进 .env 长期切换')
    p.add_argument('action', choices=['list', 'use'])
    p.add_argument('--memory', dest='use_memory', help='记忆策略, 如 default / work')
    p.add_argument('--comms', dest='use_comms', help='交互协议, 如 default / work; 不写时跟着记忆策略配套的走')
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_plugin)
    # 插件自己的子命令(如 larkx memory、larkx group), 没启用时调用会提示怎么启用
    plugins.register_cli(sub)
    # 在解析参数之前拦截, 参数写错时也不会绕过
    sub_cmd = next((a for a in sys.argv[1:] if not a.startswith('-')), None)
    if is_agent_actor() and sub_cmd in AGENT_FORBIDDEN:
        sys.exit(_fail(argparse.Namespace(json='--json' in sys.argv), f'数字人不能使用 larkx {sub_cmd}', code=3))
    args, extra = parser.parse_known_args()
    if extra and args.cmd in ('send', 'doc', 'task', 'say'):
        # send 的内容可以写在选项后面, 如 send <chat> --json -
        args.text += extra
    elif extra:
        parser.error(f'无法识别的参数: {" ".join(extra)}')
    # 数字人起的命令都要过访客检查: 不只看 LARKX_ACTOR(env -u 去掉它也认得出, 见 inside_twin);
    # daemon 自己(同一个服务单元里)跑的 daemon/listen 不算
    if is_agent_actor() or (args.cmd not in SERVICE_CMDS and inside_twin()):
        denied = _guest_denied(args)
        if denied:
            sys.exit(_fail(args, denied, code=3))
    sys.exit(args.fn(args))


# 服务本身(systemd 单元里)启动时跑的子命令
SERVICE_CMDS = ('daemon', 'listen', 'web')


def _guest_denied(args) -> str:
    """访客发起的这一轮(不是本人发起): 只能用少数子命令, 只能看当前会话; 被拦下时通知本人。
    读不到这一轮或这一轮不可信(turn.json 被改过、没签名)时也按访客处理, 只能更严。"""
    from . import guest
    turn = state.agent_turn()
    if not guest.is_guest(turn):
        return ''
    st = Storage()
    denied = guest.cli_denied(args.cmd, args, turn, st)
    if denied:
        from .notify import Notifier
        st.log_action(actor='agent', action=args.cmd, chat_id=turn.get('chat_id', ''), content=' '.join(sys.argv[1:])[:300],
                      ok=0, error=denied, turn_kind=turn.get('kind', ''), turn_ref=turn.get('ref', ''))
        Notifier(load_config(), LarkClient(LarkAuth())).alert(
            f'访客发起的请求({turn.get("ref")})想让数字人执行 larkx {args.cmd}, 已拦截: {denied}',
            key=f'guest:{args.cmd}:{turn.get("ref")}', cooldown=600)
    return denied


if __name__ == '__main__':
    main()
