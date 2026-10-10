"""Human-facing commands for virtual reward points and free raffle events."""
from .i18n import msg as _ui, join_text
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
            bot.send(cid, _ui('请回复同群真实成员的一条消息，再发送 /transfer 20'))
            return True
        if not _member(bot, cid, uid) or not _member(bot, cid, recipient):
            bot.send(cid, _ui('无法核实当前群成员身份，转账没有发起。'))
            return True
        try:
            amount = _number(argument, _ui('格式：回复成员 /transfer 20（1–1000）'),
                             maximum=1000)
            intent = bot.economy.new_transfer(
                cid, uid, recipient, amount, message["message_id"])
        except ValueError as exc:
            bot.send(cid, _ui('⚠️ {p0}', p0=exc))
            return True
        if intent["status"] != "pending" or intent["confirmation_message_id"]:
            return True
        text = (_ui('🪙 积分转账二次确认\n发起人：{p0}\n接收人：{p1}\n金额：{p2} 积分\n仅发起人可以确认，5 分钟后失效。', p0=uid, p1=recipient, p2=amount))
        buttons = [[
            {"text": _ui('✅ 确认转账'),
             "callback_data": _ui('et:{p0}:{p1}:{p2}', p0=cid, p1=intent['id'], p2=intent['nonce'])},
            {"text": _ui('❌ 取消'),
             "callback_data": _ui('ec:{p0}:{p1}:{p2}', p0=cid, p1=intent['id'], p2=intent['nonce'])},
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
            text = _ui('🎁 本群积分商城（仅虚拟群内奖励，管理员手动发放）\n') + (
                join_text('\n', (_ui('#{p0} {p1} · {p2} 积分 · 余 {p3}', p0=r['id'], p1=r['title'], p2=r['price'], p3=r['stock']) for r in items)) or _ui('暂未上架奖励'))
            text += _ui('\n兑换请发送 /buy 商品编号；查询订单 /myorders')
            bot.send(cid, text)
        elif command == "buy":
            item_id = _number(argument, _ui('格式：/buy 商品编号'), maximum=2_000_000_000)
            result = bot.economy.purchase(cid, uid, item_id, message["message_id"])
            if result["created"]:
                bot.send(cid, _ui('✅ 兑换申请 #{p0}：{p1}\n剩余积分 {p2}，待管理员人工核销。', p0=result['id'], p1=result['title'], p2=result['balance']))
                bot.log(cid, _ui('商城新订单 #{p0}', p0=result['id']), actor=uid)
        elif command == "myorders":
            rows = bot.economy.orders(cid, user_id=uid, limit=10)
            status_names = {"pending": _ui('待处理'), "fulfilled": _ui('已核销'), "refunded": _ui('已退款')}
            bot.send(cid, _ui('🎁 我的最近订单\n') + (
                join_text('\n', (_ui('#{p0} {p1} · {p2}积分 · {p3}', p0=r['id'], p1=r['item_title'], p2=r['cost'], p3=status_names[r['status']]) for r in rows)) or _ui('暂无订单')))
        elif command == "raffle":
            current = bot.raffles.active(cid)
            if current is None:
                bot.send(cid, _ui('🎉 本群当前没有开放的免费抽奖。无需积分或现金报名。'))
            else:
                bot.send(cid, _ui('🎉 免费活动 #{p0}：{p1}\n无需付费、无需积分。发送 /joinraffle 免费报名。', p0=current['id'], p1=current['title']))
        elif command == "joinraffle":
            if not _member(bot, cid, uid):
                bot.send(cid, _ui('无法确认群成员资格，本次未报名。'))
                return True
            result = bot.raffles.join(cid, uid)
            if result["joined"]:
                bot.send(cid, _ui('🎉 已报名免费抽奖 #{p0}，当前 {p1} 人参与。', p0=result['raffle_id'], p1=result['count']), disable_notification=True)
            else:
                bot.send(cid, _ui('你已经报名过本次免费抽奖，无需重复报名。'))
    except ValueError as exc:
        bot.send(cid, _ui('⚠️ {p0}', p0=exc))
    return True


def handle_admin(bot, message, command, argument):
    cid = message["chat"]["id"]
    actor = message["from"]["id"]
    try:
        if command == "shopadd":
            chunks = [p.strip() for p in argument.split("|", 3)]
            if len(chunks) != 4:
                raise ValueError(_ui('格式：/shopadd 名称 | 单价 | 库存 | 说明'))
            title, raw_price, raw_stock, description = chunks
            price = _number(raw_price, _ui('单价需为 1–10000 积分'))
            stock = _number(raw_stock, _ui('库存需为 1–1000'), maximum=1000)
            item_id = bot.economy.add_item(cid, actor, title, description,
                                           price, stock)
            bot.send(cid, _ui('✅ 上架奖励 #{p0}：{p1}，{p2} 积分，库存 {p3}', p0=item_id, p1=title, p2=price, p3=stock))
            bot.log(cid, _ui('上架奖励 #{p0}', p0=item_id), actor=actor)
        elif command == "shopremove":
            item_id = _number(argument, _ui('格式：/shopremove 编号'), maximum=2_000_000_000)
            if not bot.economy.disable_item(cid, item_id):
                raise ValueError(_ui('该奖励不存在或已下架'))
            bot.send(cid, _ui('✅ 奖励 #{p0} 已下架（不影响已有订单）', p0=item_id))
        elif command == "shoporders":
            orders = bot.economy.orders(cid, status="pending", limit=25)
            bot.send(cid, _ui('📦 待人工核销订单\n') + (
                join_text('\n', (_ui('#{p0} 用户{p1} {p2}', p0=r['id'], p1=r['member_id'], p2=r['item_title']) for r in orders)) or _ui('没有待处理订单')))
        elif command in ("shopdone", "shoprefund"):
            order_id = _number(argument, _ui('格式：/{p0} 订单编号', p0=command),
                               maximum=2_000_000_000)
            action = "fulfill" if command == "shopdone" else "refund"
            result = bot.economy.process_order(cid, order_id, actor, action)
            if result["changed"]:
                verb = _ui('已人工核销') if action == "fulfill" else _ui('已退款并恢复库存')
                bot.send(cid, _ui('✅ 订单 #{p0} {p1}', p0=order_id, p1=verb))
                bot.log(cid, _ui('商城订单 #{p0} {p1}', p0=order_id, p1=result['status']),
                        actor=actor, target=result["member_id"])
        elif command == "rafflestart":
            raffle_id = bot.raffles.create(cid, actor, argument)
            bot.send(cid, _ui('🎉 免费抽奖 #{p0}：{p1}\n群成员发送 /joinraffle 免费报名；不消耗积分、不涉及付费。', p0=raffle_id, p1=argument.strip()))
            bot.log(cid, _ui('免费抽奖 #{p0} 已创建', p0=raffle_id), actor=actor)
        elif command == "raffledraw":
            if argument:
                raise ValueError(_ui('用法：/raffledraw'))
            result = bot.draw_raffle(cid, actor)
            bot.send(cid, _ui('🎉 免费抽奖 #{p0} 已开奖！\n活动：{p1}\n参与：{p2} 人\n中奖用户 ID：{p3}\n每人只有一次报名机会，中奖结果不可重抽。', p0=result['id'], p1=result['title'], p2=result['count'], p3=result['winner_id']))
            bot.log(cid, _ui('免费抽奖 #{p0} 中奖 {p1}', p0=result['id'], p1=result['winner_id']),
                    actor=actor)
        elif command == "rafflecancel":
            if argument:
                raise ValueError(_ui('用法：/rafflecancel'))
            changed = bot.raffles.cancel(cid, actor)
            if not changed:
                raise ValueError(_ui('本群没有正在开放的抽奖'))
            bot.send(cid, _ui('✅ 本群免费抽奖已取消（不收取积分，无需退款）'))
        elif command == "rafflehistory":
            records = bot.raffles.history(cid)
            bot.send(cid, _ui('🎉 本群免费抽奖记录\n') + (
                join_text('\n', (_ui('#{p0} {p1} [{p2}] 报名 {p3} · 中奖 {p4}', p0=r['id'], p1=r['title'], p2=r['status'], p3=r['count'], p4=r['winner_id'] or '—') for r in records)) or _ui('暂无')))
    except ValueError as exc:
        bot.send(cid, _ui('⚠️ {p0}', p0=exc))
