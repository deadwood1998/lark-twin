import gzip
import json
import zlib
from urllib.parse import parse_qs
from protobuf_to_dict import protobuf_to_dict
from . import proto_pb2 as P
from .gateway import _lark_all
MSG_TYPE_NAMES = {
    0: 'UNKNOWN', 2: 'POST', 3: 'FILE', 4: 'TEXT', 5: 'IMAGE', 6: 'SYSTEM',
    7: 'AUDIO', 8: 'EMAIL', 9: 'SHARE_GROUP_CHAT', 10: 'STICKER',
    11: 'MERGE_FORWARD', 12: 'CALENDAR', 13: 'CLOUD_FILE', 14: 'CARD',
    15: 'MEDIA', 16: 'SHARE_CALENDAR_EVENT', 17: 'HONGBAO',
    18: 'GENERAL_CALENDAR', 19: 'VIDEO_CHAT', 20: 'LOCATION',
    22: 'COMMERCIALIZED_HONGBAO', 23: 'SHARE_USER_CARD', 24: 'TODO', 25: 'FOLDER',
}
CHAT_TYPE_NAMES = {0: 'UNKNOWN', 1: 'P2P', 2: 'GROUP', 3: 'TOPIC_GROUP'}
# Chat.ChatMode 的 THREAD / THREAD_V2;新建的话题群是 GROUP + 这两种模式,旧版话题群才是 TOPIC_GROUP
THREAD_CHAT_MODES = (2, 3)


def enum_to_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        names = P.entities.Message.Type.keys()
        if value in names:
            return P.entities.Message.Type.Value(value)
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def from_type(value) -> int:
    """消息的 fromType: 1 是用户, 2 是机器人(应用); 解码结果可能是数字也可能是枚举名。
    实测机器人发的私聊也标成 1, 要确认是不是机器人用 LarkClient.is_bot。"""
    if isinstance(value, int):
        return value
    names = {'USER': 1, 'BOT': 2}
    if isinstance(value, str):
        if value in names:
            return names[value]
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def chatter_is_bot(value) -> bool:
    """Chatter.type: 1 是用户, 2 是机器人, 3 是 AI 助手; 解码结果可能是数字也可能是枚举名。"""
    if isinstance(value, str):
        value = {'USER': 1, 'BOT': 2, 'AI': 3}.get(value, value)
    try:
        return int(value) in (2, 3)
    except (TypeError, ValueError):
        return False


def is_thread_chat(chat_info: dict) -> bool:
    """话题群里每条根消息自成一个话题。chat_info 为 chats.PullChatsByIdsRequest 返回的单个会话。"""
    return enum_to_int(chat_info.get('chatMode', 0)) in THREAD_CHAT_MODES or enum_to_int(chat_info.get('type', 0)) == 3


def resolve_scope(msg_id: str, msg_type: int, chat_type: int, root_id: str, thread_id: str, thread_chat: bool=False) -> tuple:
    """返回 (scope, anchor):anchor 是回复时的 --root,也是 agent 的会话边界。"""
    if chat_type == 1:
        return ('chat', '')
    if root_id and root_id != '0':
        return ('topic', thread_id or root_id)
    # 话题群的根消息本身就开启一个话题(建群等系统提示除外);普通群每条消息也带 threadId=自身 id,不能据此判断
    if (thread_chat or chat_type == 3) and msg_type != 6:
        return ('topic', msg_id or '')
    return ('chat', '')


def extract_rich_text(rich_text_dict):
    """把 richText 元素树展开成纯文本摘要。

    富文本有两种布局: 老的扁平式(叶子直接挂在 elementIds 下)和树形(DOCS(22) 下挂 P/UL/OL/LI/QUOTE/
    代码块等块级元素, 文字在叶子里), 都按 childIds 递归走一遍。块级元素之间补换行, 列表项补
    "- "/"n. "前缀, 代码块(31/37)取代码内容。解码失败的元素按占位符处理。
    """
    TAG_PLACEHOLDERS = {2: '[图片]', 10: '[表情]', 18: '[视频]'}
    TEXT_TAGS = (1, 7, 8, 9, 17, 23, 24, 25, 44, 45, 46)
    BLOCK_TAGS = (3, 26, 27, 28, 29, 31, 37, 23, 24, 25, 44, 45, 46)
    AT, A, OL, LI, CODE_BLOCK, CODE_BLOCK_V2, P_TAG = 5, 6, 27, 28, 31, 37, 3
    E = _lark_all().entities.RichTextElement
    text = ''
    try:
        dictionary = rich_text_dict['elements']['dictionary']

        def render_tag(tag, prop):
            nonlocal text
            if not prop:
                return
            if tag in TEXT_TAGS:
                tp = P.TextProperty()
                try:
                    tp.ParseFromString(prop)
                    text += tp.content
                except Exception:
                    pass
                return
            if tag == AT:
                ap = P.AtProperty()
                try:
                    ap.ParseFromString(prop)
                    text += ap.content or '[@]'
                except Exception:
                    text += '[@]'
                return
            if tag == A:
                # 现在的链接属性是 AnchorProperty(field1 href/field2 content/field9 isCustom/field10 scene);
                # 老消息用 LinkProperty(field1 url/field2 iosUrl)。两种 field1 都是 string, Anchor 能直接读出
                # href, 但老消息的 iosUrl 会被误读成 content, 所以只在有自定义链接标记(isCustom/scene)时
                # 才把 content 当自定义文字, 否则只输出 url。Anchor 读不到时再回退 LinkProperty。
                ap = E.AnchorProperty()
                try:
                    ap.ParseFromString(prop)
                except Exception:
                    ap = None
                href = ap.href if ap is not None else ''
                if not href:
                    lp = E.LinkProperty()
                    try:
                        lp.ParseFromString(prop)
                        href = lp.url or ''
                    except Exception:
                        href = ''
                if href:
                    content = ap.content if ap is not None else ''
                    if (ap is not None and (ap.isCustom or ap.scene)
                            and content and content != href):
                        text += f'{content}({href})'
                    else:
                        text += href
                return
            if tag == CODE_BLOCK:
                cb = E.CodeBlockProperty()
                try:
                    cb.ParseFromString(prop)
                    text += cb.content
                except Exception:
                    pass
                return
            if tag == CODE_BLOCK_V2:
                cb = E.CodeBlockV2Property()
                try:
                    cb.ParseFromString(prop)
                    for line in cb.contents:
                        text += ''.join(c.content for c in line.contents) + '\n'
                except Exception:
                    pass
                return
            text += TAG_PLACEHOLDERS.get(tag, '')

        seen = set()

        def walk(key, ordered=False, index=0):
            nonlocal text
            if key in seen:
                return
            seen.add(key)
            v = dictionary.get(key)
            if not isinstance(v, dict):
                return
            tag = enum_to_int(v.get('tag', 1))
            prop = v.get('property')
            children = v.get('childIds') or []
            if tag == LI:
                text += f'{index + 1}. ' if ordered else '- '
            if children:
                if tag in BLOCK_TAGS and tag != LI and text and not text.endswith('\n'):
                    text += '\n'
                for i, child in enumerate(children):
                    walk(child, ordered=(tag == OL), index=i)
                if tag in BLOCK_TAGS and not text.endswith('\n'):
                    text += '\n'
            else:
                render_tag(tag, prop)
                if tag == P_TAG and not text.endswith('\n'):
                    text += '\n'

        element_ids = rich_text_dict.get('elementIds') or []
        roots = [k for k in element_ids if k in dictionary]
        # 扁平布局或漏挂的元素: 按 id 顺序补在后面
        roots_set = set(roots)
        roots += [k for k in sorted(dictionary, key=lambda x: int(x) if str(x).isdigit() else 0)
                  if k not in roots_set]
        for root in roots:
            walk(root)
    except Exception:
        pass
    return text.strip()


def decode_message_content(message_type, content_bytes):
    try:
        if message_type == 4:
            tc = P.TextContent()
            tc.ParseFromString(content_bytes)
            d = protobuf_to_dict(tc)
            text = extract_rich_text(d.get('richText', {})) or d.get('text', '')
            return (text, d)
        if message_type == 2:
            pc = P.PostContent()
            pc.ParseFromString(content_bytes)
            d = protobuf_to_dict(pc)
            title = d.get('title', '')
            text = extract_rich_text(d.get('richText', {})) or d.get('text', '')
            return (f'[富文本] {title}\n{text}'.strip(), d)
        if message_type == 5:
            ic = P.ImageContent()
            ic.ParseFromString(content_bytes)
            d = protobuf_to_dict(ic)
            v2 = d.get('imageV2') or {}
            if v2:
                key = v2.get('imageKey', '')
                width = height = '?'
                for variant in v2.get('variants', []):
                    if variant.get('type') == 1:
                        size = variant.get('size') or {}
                        width, height = (size.get('width', '?'), size.get('height', '?'))
                        break
                has_crypto = 'crypto' in v2
                return (f"[图片] imageKey={key} ({width}x{height}){(' 已加密' if has_crypto else '')}", d)
            img = (d.get('image') or {}).get('origin') or {}
            key = img.get('key') or (d.get('image') or {}).get('key') or (d.get('image') or {}).get('imageKey', '')
            return (f"[图片] imageKey={key} ({img.get('width', '?')}x{img.get('height', '?')})", d)
        if message_type == 3:
            fc = P.FileContent()
            fc.ParseFromString(content_bytes)
            d = protobuf_to_dict(fc)
            return (f"[文件] {d.get('name', '')} ({d.get('mime', '')}, {d.get('size', 0)}字节, key={d.get('key', '')})", d)
        if message_type == 15:
            mc = P.MediaContent()
            mc.ParseFromString(content_bytes)
            d = protobuf_to_dict(mc)
            return (f"[视频] {d.get('name', '')} (时长{d.get('duration', 0) / 1000:.1f}s, {d.get('size', 0)}字节, key={d.get('key', '')})", d)
        if message_type == 7:
            ac = P.AudioContent()
            ac.ParseFromString(content_bytes)
            d = protobuf_to_dict(ac)
            return (f"[语音] 时长{d.get('duration', 0) / 1000:.1f}s, key={d.get('key', '')}", d)
        if message_type == 10:
            sc = P.StickerContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            return (f"[表情包] stickerId={d.get('stickerId', '')} key={d.get('key', '')}", d)
        if message_type == 14:
            cc = P.CardContent()
            cc.ParseFromString(content_bytes)
            d = protobuf_to_dict(cc)
            header = d.get('cardHeader') or {}
            title = header.get('title') or header.get('mainTitle') or ''
            json_card = d.get('jsonCard', '')
            parts = ['[卡片]']
            if title:
                parts.append(title)
            if d.get('cardDesc'):
                parts.append(f"({d['cardDesc']})")
            if json_card:
                parts.append(json_card[:2000])
            elif d.get('openCardContent'):
                parts.append(f"<openCardContent {len(d['openCardContent'])}字节>")
            return (' '.join(parts), d)
        if message_type == 11:
            mfc = P.MergeForwardContent()
            mfc.ParseFromString(content_bytes)
            d = protobuf_to_dict(mfc)
            msgs = d.get('messages', [])
            name = d.get('groupChatName') or f"{d.get('p2pCreatorName', '')}与{d.get('p2pPartnerName', '')}的会话"
            lines = [f'[合并转发] {name} 共{len(msgs)}条:']
            for m in msgs[:20]:
                sub_type = enum_to_int(m.get('type', 0))
                sub_summary, _ = decode_message_content(sub_type, m.get('content', b''))
                lines.append(f"  {m.get('fromId', '')}: {sub_summary}")
            return ('\n'.join(lines), d)
        if message_type == 20:
            lc = P.LocationContent()
            lc.ParseFromString(content_bytes)
            d = protobuf_to_dict(lc)
            desc = d.get('locationDescription') or {}
            return (f"[位置] {desc.get('name', '')} {desc.get('description', '')} ({d.get('latitude', '')},{d.get('longitude', '')})", d)
        if message_type == 6:
            sc = P.SystemContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            texts = []
            for cv in (d.get('sysContentValues') or {}).values():
                for item in (cv or {}).get('items', []):
                    if item.get('text'):
                        texts.append(item['text'])
            return (f"[系统消息] type={d.get('typeNum') or d.get('type', '')} {' '.join(texts)}", d)
        if message_type == 23:
            sc = P.ShareUserCardContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            return (f"[名片] {d.get('name', '')} (userId={d.get('userId', '')})", d)
        if message_type == 9:
            sc = P.ShareGroupChatContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            chat = d.get('chat') or {}
            chat_name = chat.get('name', '')
            chat_id = d.get('chatId') or chat.get('id', '')
            return (f'[群分享] {chat_name} (chatId={chat_id})', d)
        if message_type == 16:
            sc = P.ShareCalendarEventContent()
            sc.ParseFromString(content_bytes)
            d = protobuf_to_dict(sc)
            return (f"[分享日程] 参与人数={d.get('attendeesCount', '?')}", d)
        if message_type == 17:
            hc = P.HongbaoContent()
            hc.ParseFromString(content_bytes)
            d = protobuf_to_dict(hc)
            return (f"[红包] {d.get('subject', '')}", d)
        if message_type == 12:
            cc = P.CalendarContent()
            cc.ParseFromString(content_bytes)
            d = protobuf_to_dict(cc)
            return (f"[日历] {d.get('calendar', '')}", d)
        if message_type == 19:
            vc = P.VideoChatContent()
            vc.ParseFromString(content_bytes)
            d = protobuf_to_dict(vc)
            topic = (d.get('meetingCard') or {}).get('topic', '')
            return (f'[视频会议] {topic}', d)
        if message_type == 8:
            ec = P.EmailContent()
            ec.ParseFromString(content_bytes)
            d = protobuf_to_dict(ec)
            return (f"[邮件] {d.get('title', '')} {d.get('text', '')}", d)
    except Exception as e:
        return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}消息解析失败: {e}]', None)
    return (f'[{MSG_TYPE_NAMES.get(message_type, message_type)}类型消息]', None)


def frame_payload(frame) -> bytes:
    """WS 帧里 Packet 的字节。连接时声明了 accept_encoding=gzip, 服务端可以把推送 gzip 压缩后再发(payloadEncoding
    标成 gzip, 或者没标但数据是 gzip 格式), 要先解压, 否则解不出 Packet, 整条推送会被当成坏帧丢掉。"""
    encoding = (frame.payloadEncoding or '').lower()
    if encoding == 'gzip' or (not encoding and frame.payload[:2] == b'\x1f\x8b'):
        return gzip.decompress(frame.payload)
    if encoding in ('deflate', 'zlib'):
        return zlib.decompress(frame.payload)
    if encoding and encoding not in ('identity', 'pb'):
        raise ValueError(f'不认识的 payloadEncoding: {encoding}')
    return frame.payload


def parse_ws_frame(raw: bytes):
    frame = P.Frame()
    frame.ParseFromString(raw)
    frame_d = protobuf_to_dict(frame)
    if (frame.payloadEncoding or '').lower() == 'json':
        return (frame_d, json_packet(frame.payload))
    packet = P.Packet()
    packet.ParseFromString(frame_payload(frame))
    packet_d = protobuf_to_dict(packet)
    return (frame_d, packet_d)


def json_packet(payload: bytes) -> dict:
    """payloadEncoding=json 的帧: payload 是一段 JSON, 不是 Packet 的 protobuf。返回 {'json': 解出来的内容, 'cmd': JSON
    顶层的 cmd/command(没有时为 None)}。不带 sid: 这种帧不回 ack, 也不当新消息处理, 只用来计数和观察日志。"""
    data = json.loads(payload)
    cmd = None
    if isinstance(data, dict):
        for key in ('cmd', 'command'):
            try:
                cmd = int(data[key])
                break
            except (KeyError, TypeError, ValueError):
                continue
    return {'json': data, 'cmd': cmd}


def push_sids(raw: bytes) -> list:
    """PUSH_SID(cmd 6000)帧: 服务端只推来 sid, 完整的推送包要按 sid 再拉。返回要拉的 sid: 包自己的 sid,
    加上合并在一起推来的 compactedSids(proto_pb2.Packet 里没有这个字段, 按 improto.Packet 解)。"""
    frame = P.Frame()
    frame.ParseFromString(raw)
    packet = _lark_all().improto.Packet()
    packet.ParseFromString(frame_payload(frame))
    return list(dict.fromkeys(s for s in (packet.sid, *map(str, packet.compactedSids)) if s))


def decode_push_messages(raw: bytes):
    _, packet = parse_ws_frame(raw)
    return push_messages(packet.get('payload') or b'')


def push_messages(payload: bytes):
    """新消息推送(PushMessagesRequest)里的消息。"""
    out = []
    if not payload:
        return out
    pmr = P.PushMessagesRequest()
    pmr.ParseFromString(payload)
    pmr_d = protobuf_to_dict(pmr)
    at_me_map = pmr_d.get('messagesAtMe') or {}
    for k, v in (pmr_d.get('messages') or {}).items():
        out.append(message_dict(v, at_me=bool(at_me_map.get(v.get('id')) or at_me_map.get(k))))
    return out


def message_dict(v: dict, at_me: bool = False) -> dict:
    """消息实体(entities.Message 解成的 dict)转成入库、判断触发用的消息, 和新消息推送里的一样。
    推送里没有会话模式, 话题群的根消息由 LarkClient._dispatch 查过 chatMode 再归到它自己的话题。"""
    message_type = enum_to_int(v.get('type', 0))
    chat_type = enum_to_int(v.get('chatType', 0))
    summary, data = decode_message_content(message_type, v.get('content', b''))
    root_id = v.get('rootId') or ''
    scope, anchor = resolve_scope(v.get('id'), message_type, chat_type, root_id, v.get('threadId') or '')
    return {'msg_id': v.get('id'), 'msg_type': message_type, 'msg_type_name': MSG_TYPE_NAMES.get(message_type, str(message_type)), 'from_id': v.get('fromId'), 'from_type': from_type(v.get('fromType')), 'chat_id': v.get('chatId') or v.get('channelId'), 'chat_type': chat_type, 'chat_type_name': CHAT_TYPE_NAMES.get(chat_type, str(chat_type)), 'scope': scope, 'anchor': anchor, 'at_me': at_me, 'root_id': root_id, 'parent_id': v.get('parentId') or '', 'cid': v.get('cid') or '', 'position': v.get('position'), 'create_time': v.get('createTime'), 'content': summary, 'content_data': data}


def batch_copy_messages(payload: bytes) -> tuple:
    """逐条转发推送(messages.PushBatchCopyMessages, cmd 134): 返回 (这批消息的 id, 推送里直接带着的消息)。
    推送里只有 messageIds、会话的 feedCard 和会话最后一条消息 lastMessage(完整的消息实体), 别的消息要按 id 再拉。
    lastMessage 在这批里才用; 没有 messageIds 时就是 lastMessage 这一条。"""
    p = _lark_all().messages.PushBatchCopyMessages()
    p.ParseFromString(payload)
    ids = list(dict.fromkeys(str(i) for i in p.messageIds if i))
    last = p.lastMessage if p.HasField('lastMessage') and p.lastMessage.id else None
    if last is None or (ids and last.id not in ids):
        return ids, []
    return ids or [last.id], [message_dict(protobuf_to_dict(last))]


# 云文档通知(docs.PushDocFeedRequest, cmd 14004): 文档里 @本人、评论本人的文档、回复本人参与的评论时推来, 不是 IM 消息。
# 每篇文档是一个 feed, 入库时当成一个虚拟会话: chat_id 是 "doc:<feed id>", chat_type 用 IM 里没有的 DOC_CHAT_TYPE
DOC_CHAT_TYPE = 101
DOC_CHAT_PREFIX = 'doc:'
# DocMessage.Type 里当成消息的: 正文里 @、评论、回复; 解决评论、表情回应、撤回这些只是状态变化, 不入库
DOC_MSG_TYPES = {1: 'DOC_MENTION', 2: 'DOC_COMMENT', 3: 'DOC_REPLY'}
# PushDocFeedRequest.Status: NEW / UPDATE / CREATE 带着新动静; DELETE、READ(本人在别的端看过了)不用管
DOC_FEED_LIVE = (1, 2, 5)


def is_doc_chat(chat_id) -> bool:
    return str(chat_id or '').startswith(DOC_CHAT_PREFIX)


def doc_feed_push(payload: bytes) -> tuple:
    """云文档通知推送: 返回 (feed, {消息 id: 消息}), 都是解成 dict 的; 不用处理的状态返回 (None, {})。
    推送里可能不带消息本身, 只有 feed 的 lastMessageId, 那样由 LarkClient 按 feed id 只读地拉回来。"""
    p = _lark_all().docs.PushDocFeedRequest()
    p.ParseFromString(payload)
    if p.status not in DOC_FEED_LIVE or not p.docFeed.id:
        return None, {}
    return protobuf_to_dict(p.docFeed), {k: protobuf_to_dict(v) for k, v in p.docMessages.items()}


def doc_feed_summary(payload: bytes) -> str:
    """云文档通知推送的一行摘要(只用于日志): 状态、哪篇文档、最新消息, 推送里带着的消息是谁发的什么类型。"""
    p = _lark_all().docs.PushDocFeedRequest()
    p.ParseFromString(payload)
    status = p.DESCRIPTOR.fields_by_name['status'].enum_type.values_by_number.get(p.status)
    msgs = ', '.join(f'{k}({DOC_MSG_TYPES.get(v.type, v.type)} from {v.fromId})' for k, v in p.docMessages.items()) or '无'
    return (f'{status.name if status else p.status} feed {p.docFeed.id}「{p.docFeed.title}」 lastMessageId {p.docFeed.lastMessageId or "无"}, '
            f'带着的消息: {msgs}')


def doc_feed_messages(feed: dict, messages: dict) -> list:
    """一篇文档的 feed 和它的通知消息转成入库、判断触发用的消息, 字段和 IM 消息一样, 按时间排好。

    评论 ID 不在消息里, 在 feed 的链接上(?preview_comment_id=), 指的是 feed 最新的那条(lastMessageId); 实测用它能直接回复。
    anchor 放评论 ID(一条评论和它下面的回复就像一个话题), 别的消息拿不到评论 ID, anchor 留空, 只入库不触发。
    content_data 里的 richText 和 IM 消息的结构一样, 判断 @ 了谁可以直接用。"""
    url, _, query = (feed.get('url') or '').partition('?')
    comment_id = (parse_qs(query).get('preview_comment_id') or [''])[0]
    last = str(feed.get('lastMessageId') or '')
    doc = {'feed_id': str(feed.get('id') or ''), 'token': feed.get('token') or '', 'title': feed.get('title') or '', 'url': url}
    out = []
    for v in messages.values():
        t = enum_to_int(v.get('type', 0))
        if t not in DOC_MSG_TYPES or not v.get('fromId'):
            continue
        anchor = comment_id if str(v.get('id')) == last else ''
        out.append({'msg_id': str(v.get('id')), 'msg_type': t, 'msg_type_name': DOC_MSG_TYPES[t], 'from_id': v.get('fromId'),
                    'from_type': 0, 'chat_id': DOC_CHAT_PREFIX + doc['feed_id'], 'chat_type': DOC_CHAT_TYPE,
                    'chat_type_name': 'DOC', 'chat_name': doc['title'], 'scope': 'topic' if anchor else 'chat',
                    'anchor': anchor, 'at_me': bool(v.get('isAtMe')), 'root_id': anchor, 'parent_id': '', 'cid': '',
                    'position': 0, 'create_time': v.get('createTime'), 'content': v.get('content') or '',
                    'content_data': {'richText': v.get('richText') or {}, 'doc': dict(doc, comment_id=anchor)}})
    return sorted(out, key=lambda m: int(m.get('create_time') or 0))


def frame_diag(raw: bytes) -> str:
    """解不开的 WS 帧的诊断信息(只用于告警日志): 原始字节开头 64 字节的 hex, 再试着当外层 Frame 解, 列出解得出的字段
    和 payload 的开头(gzip 的再解压看一眼)。"""
    head = f'前 64 字节 hex: {raw[:64].hex()}'
    try:
        frame = P.Frame()
        frame.ParseFromString(raw)
    except Exception as e:
        return f'{head}; 当外层 Frame 也解不开: {e!r}'
    headers = {h.key: h.value for h in frame.headers}
    payload = frame.payload
    text = (f'{head}; 当外层 Frame 解: seqid {frame.seqid}, logid {frame.logid}, service {frame.service}, method {frame.method}, '
            f'headers {str(headers)[:300]}, payloadEncoding {frame.payloadEncoding!r}, payloadType {frame.payloadType!r}, '
            f'payload {len(payload)} 字节, 开头 hex: {payload[:32].hex()}')
    if (frame.payloadEncoding or '').lower() == 'gzip' or payload[:2] == b'\x1f\x8b':
        try:
            inner = gzip.decompress(payload)
            text += f', gzip 解压后 {len(inner)} 字节, 开头 hex: {inner[:32].hex()}'
        except Exception as e:
            text += f', gzip 解压失败: {e!r}'
    return text
