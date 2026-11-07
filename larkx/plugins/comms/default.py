"""上游 lark-twin 的交互方式: 只处理被叫到的消息(/ai、接管、定时任务), 别人发起的一律按访客收窄。"""
from .base import CommsProtocol


class DefaultComms(CommsProtocol):
    name = 'default'
    description = '上游原样: 只处理被叫到的消息, 别人发起的按访客收窄'
