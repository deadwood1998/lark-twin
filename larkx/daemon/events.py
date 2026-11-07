"""排给数字人的一件事(消息、定时任务、终端指令、维护轮)。单独一个模块, 插件也能用, 不用引入整个 daemon。"""
from dataclasses import dataclass


@dataclass
class Event:
    kind: str
    prompt: str
    chat_id: str = ''
    anchor: str = ''
    is_owner: bool = False
    ref: str = ''
    task_id: int = 0
    msg: dict = None
    body: str = ''
    chat_name: str = ''
    sender_name: str = ''
    inbox_id: int = 0
    note: str = ''
    run_id: int = 0  # 例行任务的运行记录
    listen: bool = False   # 旁听收到的(没人叫数字人): 交互协议决定(larkx/plugins/comms/)
    watch: bool = False    # 本人在线(larkx watch): 别人发起的这一轮只判断、写收件箱
    judge: bool = False    # 只因为本人在线才交给数字人的(私聊、@本人、没权限唤起): 本人下线了就不处理
    dev: bool = False      # 这一轮按开发模式放开: 交互协议按当前会话配置算出来
    meta: dict = None      # 插件自己的标记, 如记忆策略的消化轮 {'digest': (...)}
