"""Member self-service in private chat; each click checks current group membership."""
import secrets
import time


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
        return (person.get("status") in ("creator","administrator","member") or
                person.get("status")=="restricted" and person.get("is_member") is True)

    def render(self,cid,uid,mid=None):
        if not self.member(cid,uid):
            return self.bot.send(uid,"无法核实群成员身份，或尚未完成新人验证。")
        return self._home_checked(cid,uid,mid)

    def callback(self,q):
        uid,qid = q["from"]["id"],q["id"]
        msg = q.get("message") or {}
        if (msg.get("chat") or {}).get("type")!="private" or (msg.get("chat") or {}).get("id")!=uid:
            return self.bot.answer(qid,"请在本人私聊中操作。",alert=True)
        p = q.get("data","").split(":")
        try:
            if len(p)!=4:
                raise ValueError("按钮参数无效。")
            cid,op,arg = int(p[1]),p[2],p[3]
            if not self.member(cid,uid):
                raise ValueError("当前不是群成员或验证未完成。")
            now = time.monotonic()
            key = (cid,uid,op)
            if now-self.last.get(key,-1e10)<1:
                return self.bot.answer(qid,"请稍候再试。")
            self.last[key] = now
            if len(self.last)>15000:
                self.last = {k:v for k,v in self.last.items() if now-v<60}
            bot,store,mid = self.bot,self.bot.store,msg.get("message_id")
            if op not in ("quote", "buy"):
                self.quotes.pop((cid,uid),None)
            b = bot._button
            back = [[b("⬅️ 成员服务",f"pc:{cid}:home:0")]]
            text,kb = "",[]
            if op=="home":
                bot.answer(qid)
                # Membership was checked above, don't add a second network RTT.
                return self._home_checked(cid,uid,mid)
            elif op=="rules":
                text = "📖 群规\n"+store.settings(cid)["rules_text"]
            elif op=="id":
                text = f"🆔 你的ID：{uid}\n群组ID：{cid}"
            elif op=="checkin":
                result = store.checkin(cid,uid)
                text = (f"✅ 签到获得 {result['award']} 分" if result["created"] else "📌 今日已签到") + f"\n当前余额 {result['balance']}"
            elif op=="balance":
                data = store.point_balance(cid,uid)
                text = f"🪙 积分 {data['balance']} · 累计签到 {data['checkins']} 天"
            elif op=="top":
                text = "🏆 积分榜\n"+("\n".join(f"{n}. {r['user_id']} · {r['balance']}分" for n,r in enumerate(store.point_top(cid),1)) or "暂无记录")
            elif op=="orders":
                rows = bot.economy.orders(cid,user_id=uid,limit=20)
                states = {"pending":"待核销","fulfilled":"已核销","refunded":"已退款"}
                text = "🧾 最近20笔个人订单\n"+("\n".join(f"#{r['id']} {r['item_title']} · {r['cost']}分 · {states[r['status']]}" for r in rows) or "暂无订单")
            elif op=="shop":
                rows = bot.economy.items(cid)
                index = max(0,min(int(arg),max(0,(len(rows)-1)//8)))
                text = f"🎁 积分奖励 · 第{index+1}页\n点击奖励查看扣分预览，确认后兑换。"
                kb = [[b(f"#{r['id']} {r['title'][:25]} · {r['price']}分 · 余{r['stock']}",f"pc:{cid}:quote:{r['id']}")] for r in rows[index*8:index*8+8]]
                if index:
                    kb.append([b("上页",f"pc:{cid}:shop:{index-1}")])
                if len(rows)>(index+1)*8:
                    kb.append([b("下页",f"pc:{cid}:shop:{index+1}")])
            elif op=="quote":
                item = bot.economy.item(cid,int(arg))
                if not item or not item["active"] or item["stock"]<1:
                    raise ValueError("奖励已下架或售罄。")
                token = secrets.token_hex(12)
                self.quotes = {k:v for k,v in self.quotes.items() if v["expires"]>now}
                if len(self.quotes)>=2000:
                    self.quotes.pop(next(iter(self.quotes)))
                self.quotes[(cid,uid)] = {"token":token,"item":item,"expires":now+300,"mid":mid}
                text = f"🔎 兑换确认\n{item['title']}\n{item['description']}\n扣除 {item['price']} 积分；需管理员人工核销。\n五分钟内有效。"
                kb = [[b("✅ 确认扣分兑换",f"pc:{cid}:buy:{token}"),b("取消",f"pc:{cid}:cancel:0")]]
            elif op=="buy":
                quote = self.quotes.get((cid,uid))
                if not quote or quote["token"]!=arg or quote["expires"]<=now or quote["mid"]!=mid:
                    raise ValueError("兑换确认已过期或失效。")
                self.quotes.pop((cid,uid),None)
                current = bot.economy.item(cid,quote["item"]["id"])
                if current != quote["item"]:
                    raise ValueError("商品或库存已变化，请重新查看。")
                order = bot.economy.purchase(cid,uid,current["id"],1,request_key=arg)
                text = f"✅ 订单 #{order['id']} 已提交，等待管理员核销。"
            elif op=="cancel":
                self.quotes.pop((cid,uid),None)
                text = "已取消兑换。"
            elif op in ("raffle","join"):
                active = bot.raffles.active(cid)
                if not active:
                    raise ValueError("当前没有免费抽奖。")
                if op=="join":
                    if active["id"]!=int(arg):
                        raise ValueError("活动已变化，请重新查看。")
                    joined = bot.raffles.join(cid,uid)
                    text = ("✅ 报名成功" if joined["joined"] else "已报名，无需重复报名") + f" · {joined['count']}人参加"
                else:
                    text = f"🎉 #{active['id']} {active['title']}\n免费报名，不消耗积分。"
                    kb = [[b("🙋 免费报名",f"pc:{cid}:join:{active['id']}")]]
            else:
                raise ValueError("未知按钮。")
            bot.answer(qid)
            result = bot._edit_or_send(uid,mid,text,kb+back)
            if op=="quote":
                if isinstance(result,dict) and result.get("message_id"):
                    self.quotes[(cid,uid)]["mid"] = result["message_id"]
                else:
                    self.quotes.pop((cid,uid),None)
            return result
        except (ValueError,KeyError,OverflowError) as exc:
            return self.bot.answer(qid,str(exc)[:180],alert=True)

    def _home_checked(self,cid,uid,mid):
        # Render helper temporarily uses the membership evidence for this click.
        b = self.bot._button
        ops = [("✅ 签到","checkin"),("🪙 积分","balance"),("🏆 排行","top"),("🎁 商城","shop"),
               ("🧾 订单","orders"),("🎉 抽奖","raffle"),("📖 群规","rules"),("🆔 我的ID","id")]
        kb = [[b(label,f"pc:{cid}:{op}:0") for label,op in ops[i:i+2]] for i in range(0,len(ops),2)]
        return self.bot._edit_or_send(uid,mid,f"👤 {self.bot.store.chat(cid)['title']} · 成员服务\n"
            "签到、兑换、免费报名均可点击操作。\n转账请在群里回复对方 /transfer 数量，并点击确认按钮。",kb)
