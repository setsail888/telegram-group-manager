"""Member self-service in private chat; each click checks current group membership."""
from .i18n import error_text, msg as _ui, join_text
import secrets
import time
from .membership import is_current_member
from .feature_help import MEMBER_PAGES


class MemberUI:
    def __init__(self,bot):
        self.bot = bot
        self.quotes = {}
        self.last = {}

    def member(self,cid,uid):
        if not self.bot.store.chat(cid) or self.bot.store.pending(cid,uid):
            return False
        person = self.bot.api.safe("getChatMember",chat_id=cid,user_id=uid)
        if not isinstance(person,dict):
            return False
        return is_current_member(person)

    def render(self,cid,uid,mid=None):
        if not self.member(cid,uid):
            return self.bot.send(uid,_ui('无法核实群成员身份，或尚未完成新人验证。'))
        return self._home_checked(cid,uid,mid)

    def callback(self,q):
        uid,qid = q["from"]["id"],q["id"]
        msg = q.get("message") or {}
        if (msg.get("chat") or {}).get("type")!="private" or (msg.get("chat") or {}).get("id")!=uid:
            return self.bot.answer(qid,_ui('请在本人私聊中操作。'),alert=True)
        p = q.get("data","").split(":")
        try:
            if len(p)!=4:
                raise ValueError(_ui('按钮参数无效。'))
            cid,op,arg = int(p[1]),p[2],p[3]
            if not self.member(cid,uid):
                raise ValueError(_ui('当前不是群成员或验证未完成。'))
            now = time.monotonic()
            key = (cid,uid,op)
            if now-self.last.get(key,-1e10)<1:
                return self.bot.answer(qid,_ui('请稍候再试。'))
            self.last[key] = now
            if len(self.last)>15000:
                self.last = {k:v for k,v in self.last.items() if now-v<60}
            bot,store,mid = self.bot,self.bot.store,msg.get("message_id")
            if op not in ("quote", "buy"):
                self.quotes.pop((cid,uid),None)
            b = bot._button
            back = [[b(_ui('⬅️ 成员服务'),_ui('pc:{p0}:home:0', p0=cid))]]
            text,kb = "",[]
            if op=="home":
                bot.answer(qid)
                # Membership was checked above, don't add a second network RTT.
                return self._home_checked(cid,uid,mid)
            elif op=="help":
                if arg not in MEMBER_PAGES:
                    raise ValueError(_ui('未知功能说明，请重新打开功能页。'))
                bot.answer(qid)
                return bot.feature_help.member(uid,cid,arg,mid)
            elif op=="rules":
                text = _ui('📖 群规\n')+store.content_text(cid, "rules_text")
            elif op=="id":
                text = _ui('🆔 你的ID：{p0}\n群组ID：{p1}', p0=uid, p1=cid)
            elif op=="checkin":
                result = store.checkin(cid,uid)
                text = (_ui('✅ 签到获得 {p0} 分', p0=result['award']) if result["created"] else _ui('📌 今日已签到')) + _ui('\n当前余额 {p0}', p0=result['balance'])
            elif op=="balance":
                data = store.point_balance(cid,uid)
                text = _ui('🪙 积分 {p0} · 累计签到 {p1} 天', p0=data['balance'], p1=data['checkins'])
            elif op=="top":
                text = _ui('🏆 积分榜\n')+(join_text('\n', (_ui('{p0}. {p1} · {p2}分', p0=n, p1=r['user_id'], p2=r['balance']) for n, r in enumerate(store.point_top(cid), 1))) or _ui('暂无记录'))
            elif op=="orders":
                rows = bot.economy.orders(cid,user_id=uid,limit=20)
                states = {"pending":_ui('待核销'),"fulfilled":_ui('已核销'),"refunded":_ui('已退款')}
                text = _ui('🧾 最近20笔个人订单\n')+(join_text('\n', (_ui('#{p0} {p1} · {p2}分 · {p3}', p0=r['id'], p1=r['item_title'], p2=r['cost'], p3=states[r['status']]) for r in rows)) or _ui('暂无订单'))
            elif op=="shop":
                rows = bot.economy.items(cid)
                index = max(0,min(int(arg),max(0,(len(rows)-1)//8)))
                text = _ui('🎁 积分奖励 · 第{p0}页\n点击奖励查看扣分预览，确认后兑换。', p0=index + 1)
                kb = [[b(_ui('#{p0} {p1} · {p2}分 · 余{p3}', p0=r['id'], p1=r['title'][:25], p2=r['price'], p3=r['stock']),_ui('pc:{p0}:quote:{p1}', p0=cid, p1=r['id']))] for r in rows[index*8:index*8+8]]
                if index:
                    kb.append([b(_ui('上页'),_ui('pc:{p0}:shop:{p1}', p0=cid, p1=index - 1))])
                if len(rows)>(index+1)*8:
                    kb.append([b(_ui('下页'),_ui('pc:{p0}:shop:{p1}', p0=cid, p1=index + 1))])
            elif op=="quote":
                item = bot.economy.item(cid,int(arg))
                if not item or not item["active"] or item["stock"]<1:
                    raise ValueError(_ui('奖励已下架或售罄。'))
                token = secrets.token_hex(12)
                self.quotes = {k:v for k,v in self.quotes.items() if v["expires"]>now}
                if len(self.quotes)>=2000:
                    self.quotes.pop(next(iter(self.quotes)))
                self.quotes[(cid,uid)] = {"token":token,"item":item,"expires":now+300,"mid":mid}
                text = _ui('🔎 兑换确认\n{p0}\n{p1}\n扣除 {p2} 积分；需管理员人工核销。\n五分钟内有效。', p0=item['title'], p1=item['description'], p2=item['price'])
                kb = [[b(_ui('✅ 确认扣分兑换'),_ui('pc:{p0}:buy:{p1}', p0=cid, p1=token)),b(_ui('取消'),_ui('pc:{p0}:cancel:0', p0=cid))]]
            elif op=="buy":
                quote = self.quotes.get((cid,uid))
                if not quote or quote["token"]!=arg or quote["expires"]<=now or quote["mid"]!=mid:
                    raise ValueError(_ui('兑换确认已过期或失效。'))
                self.quotes.pop((cid,uid),None)
                current = bot.economy.item(cid,quote["item"]["id"])
                if current != quote["item"]:
                    raise ValueError(_ui('商品或库存已变化，请重新查看。'))
                order = bot.economy.purchase(cid,uid,current["id"],1,request_key=arg)
                text = _ui('✅ 订单 #{p0} 已提交，等待管理员核销。', p0=order['id'])
            elif op=="cancel":
                self.quotes.pop((cid,uid),None)
                text = _ui('已取消兑换。')
            elif op in ("raffle","join"):
                active = bot.raffles.active(cid)
                if op=="join":
                    if not active:
                        raise ValueError(_ui('当前没有免费抽奖。'))
                    if active["id"]!=int(arg):
                        raise ValueError(_ui('活动已变化，请重新查看。'))
                    joined = bot.raffles.join(cid, uid)
                    text = (_ui('✅ 报名成功') if joined["joined"] else _ui('已报名，无需重复报名')) + _ui(' · {p0}人参加', p0=joined['count'])
                elif active:
                    text = _ui('🎉 #{p0} {p1}\n免费报名，不消耗积分。', p0=active['id'], p1=active['title'])
                    kb = [[b(_ui('🙋 免费报名'),_ui('pc:{p0}:join:{p1}', p0=cid, p1=active['id']))]]
                else:
                    text = _ui('当前没有免费抽奖。')
            else:
                raise ValueError(_ui('未知按钮。'))
            bot.answer(qid)
            help_page = {"join": "raffle", "quote": "shop", "buy": "shop", "cancel": "shop"}.get(op, op)
            bot.feature_help.add_member(kb,cid,help_page)
            result = bot._edit_or_send(uid,mid,text,kb+back)
            if op=="quote":
                if isinstance(result,dict) and result.get("message_id"):
                    self.quotes[(cid,uid)]["mid"] = result["message_id"]
                else:
                    self.quotes.pop((cid,uid),None)
            return result
        except (ValueError,KeyError,OverflowError) as exc:
            return self.bot.answer(qid,error_text(exc)[:180],alert=True)

    def _home_checked(self,cid,uid,mid):
        # Render helper temporarily uses the membership evidence for this click.
        b = self.bot._button
        ops = [(_ui('✅ 签到'),"checkin"),(_ui('🪙 积分'),"balance"),(_ui('🏆 排行'),"top"),(_ui('🎁 商城'),"shop"),
               (_ui('🧾 订单'),"orders"),(_ui('🎉 抽奖'),"raffle"),(_ui('📖 群规'),"rules"),(_ui('🆔 我的ID'),"id")]
        kb = [[b(label,_ui('pc:{p0}:{p1}:0', p0=cid, p1=op)) for label,op in ops[i:i+2]] for i in range(0,len(ops),2)]
        self.bot.feature_help.add_member(kb,cid,"home")
        kb.append([b(_ui("🌐 界面语言 / Language"), "lang:self")])
        return self.bot._edit_or_send(uid,mid,_ui('👤 {p0} · 成员服务\n签到、兑换、免费报名均可点击操作。\n转账请在群里回复对方 /transfer 数量，并点击确认按钮。', p0=self.bot.store.chat(cid)['title']),kb)
