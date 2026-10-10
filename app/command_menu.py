"""Native Telegram slash-command descriptions, separate from command identifiers."""
from .i18n import msg as _ui, render

COMMAND_MENU = (
    ("start", _ui("打开群组管理面板")),
    ("panel", _ui("群组管理面板")),
    ("menu", _ui("成员菜单：签到、积分、兑换、抽奖、群规")),
    ("language", _ui("切换界面语言 / Change language")),
    ("help", _ui("查看指令")),
    ("rules", _ui("查看群规")),
    ("stats", _ui("今日群组数据")),
    ("statweek", _ui("最近7天群组统计")),
    ("schedule", _ui("管理员定时公告")),
    ("guard", _ui("管理员查看反垃圾、防刷屏高级设置")),
    ("words", _ui("管理员查看本群违禁词")),
    ("checkin", _ui("签到领取积分（需群管理员启用）")),
    ("points", _ui("查看自己在本群的积分")),
    ("pointtop", _ui("查看本群积分排行榜")),
    ("dban", _ui("回复广告消息静默封禁（管理员）")),
)


def install_commands(api, locale, chat_id=None, language_code=""):
    params = {"commands": [{"command": key, "description": render(description, locale)}
                           for key, description in COMMAND_MENU], "language_code": language_code}
    if chat_id is not None:
        params["scope"] = {"type": "chat", "chat_id": chat_id}
    return api.safe("setMyCommands", **params)
