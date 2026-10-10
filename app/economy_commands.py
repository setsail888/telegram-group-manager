"""Human-facing commands for virtual reward points and free raffle events."""
import time

from .api import TelegramAPIError
from .membership import is_current_member

MEMBER_COMMANDS = {"transfer", "shop", "buy", "myorders", "raffle", "joinraffle"}
ADMIN_COMMANDS = {
    "shopadd", "shopremove", "shoporders", "shopdone", "shoprefund",
    "rafflestart", "raffledraw", "rafflecancel", "rafflehistory",
}


def _number(text, example, *, maximum=10000):
    try:
        value = int(text.strip())
    except (ValueError, AttributeError):
        raise ValueError(example) from None
    if not 1 <= value <= maximum:
        raise ValueError(example)
    return value


def _member(bot, chat_id, user_id):
    try:
        info = bot.api.call("getChatMember", chat_id=chat_id, user_id=user_id)
    except TelegramAPIError:
        return False
    return is_current_member(info) and not bot.store.pending(chat_id, user_id)


def _limited(bot, chat_id, user_id, command, seconds=12):
    key = (chat_id, user_id, command)
    now = time.monotonic()
    prev = bot._public_command_time.get(key)
    if prev is not None and now - prev < seconds:
        return True
    bot._public_command_time[key] = now
    if len(bot._public_command_time) > 15000:
        for old, tick in list(bot._public_command_time.items()):
            if now - tick > 120:
                del bot._public_command_time[old]
    return False


def handle_member(bot, message, command, argument):
    cid = message["chat"]["id"]
    sender = message.get("from") or {}
    uid = sender.get("id")
    if not uid or sender.get("is_bot") or message.get("sender_chat"):
        return False
    if command == "transfer":
        if _limited(bot, cid, uid, "transfer", 15):
            return True
        target_message = message.get("reply_to_message") or {}
        target = target_message.get("from") or {}
        recipient = target.get("id")
        if not recipient or target.get("is_bot") or target_message.get("sender_chat"):
            bot.send(cid, "请回复同群真实成员的一条消息，再发送 /transfer 20")
            return True
        if not _member(bot, cid, uid) or not _member(bot, cid, recipient):
            bot.send(cid, "无法核实当前群成员身份，转账没有发起。")
            return True
        try:
            amount = _number(argument, "格式：回复成员 /transfer 20（1–1000）",
                             maximum=1000)
            intent = bot.economy.new_transfer(
                cid, uid, recipient, amount, message["message_id"])
        except ValueError as exc:
            bot.send(cid, f"⚠️ {exc}")
            return True
        if intent["status"] != "pending" or intent["confirmation_message_id"]:
            return True
        text = (f"🪙 积分转账二次确认\n"
                f"发起人：{uid}\n接收人：{recipient}\n金额：{amount} 积分\n"
                "仅发起人可以确认，5 分钟后失效。")
        buttons = [[
            {"text": "✅ 确认转账",
             "callback_data": f"et:{cid}:{intent['id']}:{intent['nonce']}"},
            {"text": "❌ 取消",
             "callback_data": f"ec:{cid}:{intent['id']}:{intent['nonce']}"},
        ]]
        posted = bot.send(cid, text, reply_markup={"inline_keyboard": buttons})
        if isinstance(posted, dict) and posted.get("message_id"):
            bot.economy.bind_prompt(cid, intent["id"], posted["message_id"])
        return True
    if argument.strip() and command not in ("buy",):
        return False
    if command in ("shop", "buy", "myorders") and not bot.store.settings(cid)["points_enabled"]:
        return True
    if _limited(bot, cid, uid, command):
        return True
    try:
        if command == "shop":
            items = bot.economy.items(cid)
            text = "🎁 本群积分商城（仅虚拟群内奖励，管理员手动发放）\n" + (
                "\n".join(f"#{r['id']} {r['title']} · {r['price']} 积分 · 余 {r['stock']}"
                          for r in items) or "暂未上架奖励")
            text += "\n兑换请发送 /buy 商品编号；查询订单 /myorders"
            bot.send(cid, text)
        elif command == "buy":
            item_id = _number(argument, "格式：/buy 商品编号", maximum=2_000_000_000)
            result = bot.economy.purchase(cid, uid, item_id, message["message_id"])
            if result["created"]:
                bot.send(cid, f"✅ 兑换申请 #{result['id']}：{result['title']}\n"
                         f"剩余积分 {result['balance']}，待管理员人工核销。")
                bot.log(cid, f"商城新订单 #{result['id']}", actor=uid)
        elif command == "myorders":
            rows = bot.economy.orders(cid, user_id=uid, limit=10)
            status_names = {"pending": "待处理", "fulfilled": "已核销", "refunded": "已退款"}
            bot.send(cid, "🎁 我的最近订单\n" + (
                "\n".join(f"#{r['id']} {r['item_title']} · {r['cost']}积分 · "
                          f"{status_names[r['status']]}" for r in rows) or "暂无订单"))
        elif command == "raffle":
            current = bot.raffles.active(cid)
            if current is None:
                bot.send(cid, "🎉 本群当前没有开放的免费抽奖。无需积分或现金报名。")
            else:
                bot.send(cid, f"🎉 免费活动 #{current['id']}：{current['title']}\n"
                         "无需付费、无需积分。发送 /joinraffle 免费报名。")
        elif command == "joinraffle":
            if not _member(bot, cid, uid):
                bot.send(cid, "无法确认群成员资格，本次未报名。")
                return True
            result = bot.raffles.join(cid, uid)
            if result["joined"]:
                bot.send(cid, f"🎉 已报名免费抽奖 #{result['raffle_id']}，"
                         f"当前 {result['count']} 人参与。", disable_notification=True)
            else:
                bot.send(cid, "你已经报名过本次免费抽奖，无需重复报名。")
    except ValueError as exc:
        bot.send(cid, f"⚠️ {exc}")
    return True


def handle_admin(bot, message, command, argument):
    cid = message["chat"]["id"]
    actor = message["from"]["id"]
    try:
        if command == "shopadd":
            chunks = [p.strip() for p in argument.split("|", 3)]
            if len(chunks) != 4:
                raise ValueError("格式：/shopadd 名称 | 单价 | 库存 | 说明")
            title, raw_price, raw_stock, description = chunks
            price = _number(raw_price, "单价需为 1–10000 积分")
            stock = _number(raw_stock, "库存需为 1–1000", maximum=1000)
            item_id = bot.economy.add_item(cid, actor, title, description,
                                           price, stock)
            bot.send(cid, f"✅ 上架奖励 #{item_id}：{title}，{price} 积分，库存 {stock}")
            bot.log(cid, f"上架奖励 #{item_id}", actor=actor)
        elif command == "shopremove":
            item_id = _number(argument, "格式：/shopremove 编号", maximum=2_000_000_000)
            if not bot.economy.disable_item(cid, item_id):
                raise ValueError("该奖励不存在或已下架")
            bot.send(cid, f"✅ 奖励 #{item_id} 已下架（不影响已有订单）")
        elif command == "shoporders":
            orders = bot.economy.orders(cid, status="pending", limit=25)
            bot.send(cid, "📦 待人工核销订单\n" + (
                "\n".join(f"#{r['id']} 用户{r['member_id']} {r['item_title']}"
                          for r in orders) or "没有待处理订单"))
        elif command in ("shopdone", "shoprefund"):
            order_id = _number(argument, f"格式：/{command} 订单编号",
                               maximum=2_000_000_000)
            action = "fulfill" if command == "shopdone" else "refund"
            result = bot.economy.process_order(cid, order_id, actor, action)
            if result["changed"]:
                verb = "已人工核销" if action == "fulfill" else "已退款并恢复库存"
                bot.send(cid, f"✅ 订单 #{order_id} {verb}")
                bot.log(cid, f"商城订单 #{order_id} {result['status']}",
                        actor=actor, target=result["member_id"])
        elif command == "rafflestart":
            raffle_id = bot.raffles.create(cid, actor, argument)
            bot.send(cid, f"🎉 免费抽奖 #{raffle_id}：{argument.strip()}\n"
                     "群成员发送 /joinraffle 免费报名；不消耗积分、不涉及付费。")
            bot.log(cid, f"免费抽奖 #{raffle_id} 已创建", actor=actor)
        elif command == "raffledraw":
            if argument:
                raise ValueError("用法：/raffledraw")
            result = bot.draw_raffle(cid, actor)
            bot.send(cid, f"🎉 免费抽奖 #{result['id']} 已开奖！\n"
                     f"活动：{result['title']}\n参与：{result['count']} 人\n"
                     f"中奖用户 ID：{result['winner_id']}\n"
                     "每人只有一次报名机会，中奖结果不可重抽。")
            bot.log(cid, f"免费抽奖 #{result['id']} 中奖 {result['winner_id']}",
                    actor=actor)
        elif command == "rafflecancel":
            if argument:
                raise ValueError("用法：/rafflecancel")
            changed = bot.raffles.cancel(cid, actor)
            if not changed:
                raise ValueError("本群没有正在开放的抽奖")
            bot.send(cid, "✅ 本群免费抽奖已取消（不收取积分，无需退款）")
        elif command == "rafflehistory":
            records = bot.raffles.history(cid)
            bot.send(cid, "🎉 本群免费抽奖记录\n" + (
                "\n".join(f"#{r['id']} {r['title']} [{r['status']}] "
                          f"报名 {r['count']} · 中奖 {r['winner_id'] or '—'}"
                          for r in records) or "暂无"))
    except ValueError as exc:
        bot.send(cid, f"⚠️ {exc}")
