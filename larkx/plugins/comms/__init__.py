"""交互协议(内容上的): 数字人和别人怎么往来。用 LARKX_COMMS=<名字> 或 larkx --comms <名字> 选;
不写时跟着记忆策略配套的协议走(如 --memory work 会同时用 work 协议)。

这里说的是对话内容上的约定, 和飞书的通信协议(larkx/protocol/)无关。
新增一个协议: 在这个目录下建子包, 继承 base.CommsProtocol, 再登记到下面的 REGISTRY。见 docs/plugins.md。
"""
from .base import CommsProtocol

REGISTRY = {
    'default': 'larkx.plugins.comms.default:DefaultComms',
    'work': 'larkx.plugins.comms.work:WorkComms',
}
