"""云文档评论: 文档通知(decoders.doc_feed_messages)入库成 "doc:<feed id>" 的虚拟会话; 读文档、看评论、在评论下回复。

网页版协议里没有读文档正文、回复评论的网关接口, 这些都走飞书开放平台, 用本机的 lark-cli 以本人身份(--as user)调:
docs +fetch 读正文, drive +batch-query-comments 看评论, drive +add-reply 回复。所以要先装好 lark-cli 并 auth login 过
(LARKX_LARK_CLI 指定命令)。lark-cli 自己解析文档链接, 知识库(wiki)里的文档也能用。回复只发纯文本; 已解决的评论、
全文评论不能回复, lark-cli 会报错。
"""
import json
import shlex
import shutil
import subprocess

from .protocol.proto.decoders import is_doc_chat

CLI_TIMEOUT = 60
# 读正文最多返回多少字, 太长的文档截断, 免得一下子塞满 agent 的上下文
READ_MAX_CHARS = 60000


class DocError(Exception):
    pass


def doc_info(storage, chat_id: str) -> dict:
    """这篇文档最近一条通知里记下的文档信息(标题、链接、token); 本地没收到过这篇文档的通知时返回空 dict。"""
    for row in reversed(storage.get_messages(chat_id, limit=20)):
        try:
            doc = json.loads(row.content_data or '{}').get('doc') or {}
        except json.JSONDecodeError:
            continue
        if doc.get('url'):
            return doc
    return {}


def _doc_url(storage, chat_id: str) -> str:
    if not is_doc_chat(chat_id):
        raise DocError(f'{chat_id} 不是云文档会话(应以 doc: 开头)')
    url = doc_info(storage, chat_id).get('url')
    if not url:
        raise DocError(f'本地没有 {chat_id} 这篇文档的链接(没收到过它的通知)')
    return url


def _check_comment_id(comment_id):
    if not str(comment_id or '').isdigit():
        raise DocError(f'评论 ID 不对: {comment_id!r}')


def _lark_cli(cfg: dict, args: list, what: str, stdin: str = None) -> dict:
    """调 lark-cli(带 --as user --json), 返回输出里的 data; 失败时抛 DocError, what 说明在做什么。"""
    argv = shlex.split(cfg.get('lark_cli') or 'lark-cli')
    if not argv or not shutil.which(argv[0]):
        raise DocError(f'找不到 lark-cli 命令 {argv[0] if argv else ""}(LARKX_LARK_CLI); 读文档、回复评论要先装好并 auth login')
    try:
        r = subprocess.run([*argv, *args, '--as', 'user', '--json'], input=stdin, capture_output=True, text=True,
                           timeout=CLI_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DocError(f'调用 lark-cli 失败: {e!r}') from e
    try:
        out = json.loads(r.stdout or '{}')
    except json.JSONDecodeError:
        out = {}
    if r.returncode != 0 or not out.get('ok'):
        detail = out.get('error') or (r.stderr or r.stdout or '').strip()
        raise DocError(f'lark-cli {what}失败: {str(detail)[:300]}')
    return out.get('data') or {}


def read(cfg: dict, storage, chat_id: str) -> str:
    """chat_id 这篇文档的正文(markdown), 太长时截断并注明。"""
    data = _lark_cli(cfg, ['docs', '+fetch', '--doc', _doc_url(storage, chat_id), '--doc-format', 'markdown'], '读文档')
    content = (data.get('document') or {}).get('content') or ''
    if len(content) > READ_MAX_CHARS:
        content = content[:READ_MAX_CHARS] + f'\n\n(文档太长, 只显示前 {READ_MAX_CHARS} 字)'
    return content


def comment(cfg: dict, storage, chat_id: str, comment_id: str) -> dict:
    """一条评论的上下文: 评论的是哪段原文(quote)、是否已解决, 下面每条回复的发送人(开放平台的 open_id)和文字。"""
    _check_comment_id(comment_id)
    data = _lark_cli(cfg, ['drive', '+batch-query-comments', '--url', _doc_url(storage, chat_id),
                           '--comment-ids', str(comment_id)], '查评论')
    item = next(iter(data.get('items') or []), None)
    if not item:
        raise DocError(f'查不到评论 {comment_id}(可能已删除)')
    replies = []
    for r in (item.get('reply_list') or {}).get('replies') or []:
        parts = []
        for el in (r.get('content') or {}).get('elements') or []:
            if el.get('type') == 'text_run':
                parts.append((el.get('text_run') or {}).get('text') or '')
            elif el.get('type') == 'person':
                parts.append('@' + ((el.get('person') or {}).get('user_id') or ''))
            elif el.get('type') == 'docs_link':
                parts.append((el.get('docs_link') or {}).get('url') or '')
        replies.append({'reply_id': r.get('reply_id'), 'from': r.get('user_id'), 'create_time': r.get('create_time'),
                        'text': ''.join(parts)})
    return {'comment_id': str(comment_id), 'quote': item.get('quote') or '', 'is_solved': bool(item.get('is_solved')),
            'replies': replies}


def reply(cfg: dict, storage, chat_id: str, comment_id: str, text: str) -> str:
    """在 chat_id 这篇文档的 comment_id 评论下以本人身份回复 text(调用方负责签名和管控), 返回回复的 id; 失败时抛 DocError。"""
    _check_comment_id(comment_id)
    url = _doc_url(storage, chat_id)
    content = json.dumps([{'type': 'text', 'text': text}], ensure_ascii=False)
    data = _lark_cli(cfg, ['drive', '+add-reply', '--url', url, '--comment-id', str(comment_id), '--content', '-'],
                     '回复评论', stdin=content)
    # 开放平台返回的是新回复本身, lark-cli 包在 data 里; 万一没带 id 也算成功, 只是本地记录和审计里没有 id
    return str(data.get('reply_id') or '')
