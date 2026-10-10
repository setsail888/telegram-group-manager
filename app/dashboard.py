"""Opt-in, authenticated, read-only Chinese dashboard on local-only Docker port.

No bot token, no Telegram API calls, no third-party JS and no mutation routes.
Deploy behind Docker's 127.0.0.1 port binding; access using an SSH tunnel.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

LOG = logging.getLogger("groupguard.dashboard")
SESSION_TTL = 12 * 60 * 60
STYLE = """
:root{color-scheme:dark;font-family:system-ui,-apple-system,"Noto Sans SC",sans-serif}
*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e5e7eb}
header{background:#111d30;border-bottom:1px solid #304256;padding:18px max(22px,calc((100vw - 1100px)/2))}
main{max-width:1100px;margin:0 auto;padding:28px 20px 80px}
h1{margin:0;font-size:24px}h2{font-size:19px;margin:20px 0 14px}
p{line-height:1.7;color:#b8c6d7}.sub{font-size:13px;margin:5px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}
.card{background:#142238;border:1px solid #2a3c54;border-radius:12px;padding:18px;min-width:0}
.metric{font-size:29px;font-weight:750;color:#e2f0ff}.tiny{color:#91accb;font-size:13px}
a{color:#a9dbff;text-decoration:none}a:hover{text-decoration:underline}
.group{display:block;margin:10px 0;padding:15px;border-radius:9px;background:#142238;border:1px solid #2a3c54}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:11px;border-bottom:1px solid #314057;font-size:14px}
th{color:#b9d2ed}td{overflow-wrap:anywhere}.muted{color:#9cb0c7}
.bar{height:11px;background:#24425c;border-radius:12px;overflow:hidden}
.bar>span{height:11px;display:block;background:#51aada;min-width:0}
.tag{border-radius:7px;background:#253a53;padding:3px 9px;display:inline-block;font-size:13px}
.on{color:#91e1bb}.off{color:#f3c38b}
form{margin:20px 0}input{background:#0e1b2c;color:white;border:1px solid #436284;border-radius:8px;padding:12px;width:min(100%,440px)}
button{border:0;border-radius:8px;background:#2278ab;padding:12px 20px;color:white;cursor:pointer}
button:hover{background:#3895c5}.error{color:#f1a7a7}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def cookie_value(token, expiry):
    """Signed expiry: replaying a stolen old cookie cannot bypass server expiry."""
    expiry = int(expiry)
    signed = f"GroupGuard-dashboard-session-v2:{expiry}".encode("ascii")
    digest = hmac.new(token.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"{expiry}.{digest}"


def dashboard_db(db_path):
    """SQLite connection is explicitly read-only, regardless of volume rights."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError("数据库尚未创建，请先启动 Telegram 机器人。")
    uri = "file:" + quote(os.path.abspath(db_path), safe="/") + "?mode=ro"
    db = sqlite3.connect(uri, timeout=4, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def page(title, body):
    return (
        '<!doctype html><html lang="zh-CN"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)} - GroupGuard</title>'
        f'<style>{STYLE}</style></head><body>'
        '<header><h1>🛡️ GroupGuard · 群卫士</h1>'
        '<p class="sub">本地只读面板 · 不提供群成员管理操作</p></header>'
        f'<main>{body}</main></body></html>'
    ).encode("utf-8")


def stat_card(label, value):
    return '<div class="card">' + f'<div class="metric">{esc(value)}</div>' + (
        f'<div class="tiny">{esc(label)}</div></div>')


def _day_ranges(tz, now=None):
    current = datetime.fromtimestamp(time.time() if now is None else now, tz)
    day = current.date()
    return day.isoformat(), (day - timedelta(days=6)).isoformat()


def _stats(db, chat_id, start, end):
    return {r["name"]: r["total"] for r in db.execute(
        "SELECT name,SUM(count) AS total FROM stats "
        "WHERE chat_id=? AND day BETWEEN ? AND ? GROUP BY name",
        (int(chat_id), start, end))}


def _group_rows(db):
    return db.execute(
        "SELECT chat_id,title FROM chats ORDER BY updated_at DESC LIMIT 100").fetchall()


def render_overview(db, tz):
    today, week_start = _day_ranges(tz)
    groups = _group_rows(db)
    body = "<h2>群组总览</h2>"
    body += ('<div class="grid">'
             + stat_card("已登记群组", len(groups))
             + '</div>')
    if not groups:
        body += "<p>还没有登记的群组。将机器人加入群组后即可查看数据。</p>"
    for group in groups:
        cid = int(group["chat_id"])
        stats = _stats(db, cid, week_start, today)
        body += ('<a class="group" href="/group?id=' + str(cid) + '">'
                 + f'📁 <strong>{esc(group["title"])}</strong> '
                 + f'<span class="tiny">（{cid}）</span> '
                 + f'<span class="tag">7天消息 {int(stats.get("messages", 0))}</span>'
                 + f' <span class="tag">删除 {int(stats.get("deleted", 0))}</span>'
                 + '</a>')
    body += '<p class="tiny">统计数据来自本地 SQLite，不代表 Telegram 实时在线人数。</p>'
    body += '<form method="post" action="/logout"><button type="submit">安全退出</button></form>'
    return page("群组总览", body)


def render_group(db, chat_id, tz):
    chat = db.execute(
        "SELECT title,settings FROM chats WHERE chat_id=?", (int(chat_id),)).fetchone()
    if chat is None:
        return None
    today, week_start = _day_ranges(tz)
    stats_today = _stats(db, chat_id, today, today)
    stats_week = _stats(db, chat_id, week_start, today)
    config = json.loads(chat["settings"] or "{}")
    body = ('<p><a href="/">← 返回群组列表</a></p>'
            f'<h2>{esc(chat["title"])}</h2>'
            f'<p class="tiny">群组 ID：{int(chat_id)} · {esc(tz.key)}</p>'
            '<h2>今日概览</h2><div class="grid">'
            + stat_card("消息", stats_today.get("messages", 0))
            + stat_card("删除", stats_today.get("deleted", 0))
            + stat_card("新加入", stats_today.get("joins", 0))
            + stat_card("管理处罚", stats_today.get("actions", 0))
            + '</div><h2>近7天</h2><div class="grid">'
            + stat_card("消息", stats_week.get("messages", 0))
            + stat_card("删除", stats_week.get("deleted", 0))
            + stat_card("新加入", stats_week.get("joins", 0))
            + stat_card("管理处罚", stats_week.get("actions", 0))
            + '</div>')
    columns = [
        ("block_links", "链接过滤"), ("anti_flood", "刷屏过滤"),
        ("word_filter_enabled", "违禁词拦截"),
        ("repeat_photo_enabled", "重复图片检测"),
        ("spam_guard_enabled", "增强反垃圾"),
        ("word_loose_match", "拆字违禁词"),
        ("media_flood_enabled", "媒体连发拦截"),
        ("auto_mute_enabled", "累计违规禁言"),
        ("name_guard_enabled", "新人超长昵称限制"),
        ("ocr_mode", "OCR 图片文字审核"),
        ("verify_enabled", "新人验证"),
        ("points_enabled", "签到积分"), ("block_new_bots", "新机器人拦截"),
    ]
    body += '<h2>功能配置（只读）</h2><div class="grid">'
    for key, label in columns:
        on = (config.get(key, "off") != "off" if key == "ocr_mode"
              else bool(config.get(key, True if key == "word_filter_enabled"
                                   else False)))
        body += ('<div class="card">'
                 + f'<span class="{"on" if on else "off"}">'
                 + f'{"● 开启" if on else "○ 关闭"}</span>'
                 + f'<p class="sub">{esc(label)}</p></div>')
    body += '</div><h2>7天消息趋势</h2><div class="card">'
    entries = {r["day"]: r["count"] for r in db.execute(
        "SELECT day,count FROM stats WHERE chat_id=? AND name='messages' "
        "AND day BETWEEN ? AND ?",
        (int(chat_id), week_start, today))}
    days = [(datetime.fromisoformat(today).date() - timedelta(days=i)).isoformat()
            for i in range(6, -1, -1)]
    peak = max([entries.get(day, 0) for day in days] + [1])
    for day in days:
        value = max(0, int(entries.get(day, 0)))
        width = min(100, round(value * 100 / peak, 1))
        body += (f'<p class="tiny">{esc(day)} · {value} 条</p>'
                 '<div class="bar"><span style="width:'
                 f'{width}%"></span></div>')
    body += '</div>'
    try:
        users = db.execute(
            "SELECT user_id,balance,checkins FROM point_balances WHERE chat_id=? "
            "AND balance>0 ORDER BY balance DESC,user_id ASC LIMIT 10",
            (int(chat_id),)).fetchall()
    except sqlite3.OperationalError:
        users = []
    body += '<h2>积分排行榜</h2><div class="card table-wrap"><table><tr><th>用户 ID</th><th>积分</th><th>累计签到</th></tr>'
    for row in users:
        body += (f'<tr><td>{int(row["user_id"])}</td>'
                 f'<td>{int(row["balance"])}</td><td>{int(row["checkins"])}</td></tr>')
    if not users:
        body += '<tr><td colspan="3">暂无记录</td></tr>'
    body += '</table></div>'
    body += '<h2>定时公告</h2><div class="card table-wrap"><table><tr><th>标题</th><th>模式</th><th>状态</th><th>下次计划</th></tr>'
    for row in db.execute(
        "SELECT title,mode,enabled,next_run FROM scheduled_messages "
        "WHERE chat_id=? ORDER BY id DESC LIMIT 20", (int(chat_id),)):
        timestamp = datetime.fromtimestamp(row["next_run"], tz).strftime("%m-%d %H:%M")
        body += (f'<tr><td>{esc(row["title"])}</td><td>{esc(row["mode"])}</td>'
                 f'<td>{"运行中" if row["enabled"] else "暂停/结束"}</td>'
                 f'<td>{esc(timestamp)}</td></tr>')
    body += '</table></div>'
    body += ('<h2>积分商城 · 待核销订单</h2>'
             '<div class="card table-wrap"><table><tr><th>订单</th><th>用户</th>'
             '<th>虚拟奖励</th><th>消耗积分</th></tr>')
    for row in db.execute(
        "SELECT id,member_id,item_title,cost FROM shop_orders "
        "WHERE chat_id=? AND status='pending' ORDER BY id DESC LIMIT 20",
        (int(chat_id),)):
        body += (f'<tr><td>#{int(row["id"])}</td><td>{int(row["member_id"])}</td>'
                 f'<td>{esc(row["item_title"])}</td><td>{int(row["cost"])}</td></tr>')
    body += '</table></div>'
    body += ('<h2>最近的免费抽奖</h2>'
             '<div class="card table-wrap"><table><tr><th>活动</th><th>状态</th>'
             '<th>中奖用户 ID</th></tr>')
    for row in db.execute(
        "SELECT id,title,status,winner_id FROM raffles "
        "WHERE chat_id=? ORDER BY id DESC LIMIT 8", (int(chat_id),)):
        body += (
            f'<tr><td>#{int(row["id"])} {esc(row["title"])}</td>'
            f'<td>{esc(row["status"])}</td>'
            f'<td>{int(row["winner_id"]) if row["winner_id"] else "—"}</td></tr>')
    body += '</table></div>'
    body += '<h2>OCR 提醒</h2><div class="grid">'
    body += stat_card("最近7天 OCR 命中", stats_week.get("ocr_hits", 0))
    body += stat_card("最近7天 OCR 删除", stats_week.get("ocr_deleted", 0))
    body += '</div>'
    body += ('<p class="tiny">仅查看数据；如需修改，请返回 Telegram 私聊管理菜单。</p>'
             '<form method="post" action="/logout"><button type="submit">安全退出</button></form>')
    return page("群组详情", body)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "GroupGuard/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Deliberately avoid logging URLs, tokens, cookies and submitted passwords.
        LOG.info("网页请求 %s -> %s", self.client_address[0],
                 str(args[1]) if len(args) > 1 else "response")

    def _headers(self, status, *, cookie=None, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _send(self, status, body, *, cookie=None):
        self._headers(status, cookie=cookie)
        self.wfile.write(body)

    def _redirect(self, path, cookie=None):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _valid_host(self):
        host = self.headers.get("Host", "").split(":")[0].lower()
        return host in ("localhost", "127.0.0.1", "[::1]")

    def _authenticated(self):
        header = self.headers.get("Cookie", "")
        for item in header.split(";"):
            key, sep, val = item.strip().partition("=")
            if sep and key == "groupguard_session":
                if not re.fullmatch(r"\d{10,12}\.[0-9a-f]{64}", val):
                    return False
                expiry_string, _ = val.split(".", 1)
                expiry = int(expiry_string)
                remaining = expiry - int(time.time())
                if remaining <= 0 or remaining > SESSION_TTL:
                    return False
                return secrets.compare_digest(
                    val, cookie_value(self.server.dashboard_token, expiry))
        return False

    def _login(self, error=""):
        warning = f'<p class="error">{esc(error)}</p>' if error else ""
        return page("管理员登录",
                    "<h2>访问本地只读控制台</h2>"
                    "<p>请输入你为面板单独配置的访问密钥，不是 BotFather Token。</p>"
                    f"{warning}<form method=\"post\" action=\"/login\">"
                    '<input name="token" type="password" required '
                    'placeholder="DASHBOARD_TOKEN" autocomplete="off" maxlength="256">'
                    '<p><button type="submit">登录面板</button></p></form>')

    def do_GET(self):
        if not self._valid_host():
            return self._send(HTTPStatus.FORBIDDEN, page("拒绝访问", "<p>无效 Host。</p>"))
        path = urlsplit(self.path)
        if path.path == "/login":
            return self._send(HTTPStatus.OK, self._login())
        if not self._authenticated():
            return self._redirect("/login")
        try:
            with closing(dashboard_db(self.server.db_path)) as db:
                if path.path == "/":
                    return self._send(HTTPStatus.OK, render_overview(db, self.server.tz))
                if path.path == "/group":
                    params = parse_qs(path.query)
                    raw = params.get("id", [""])[0]
                    if not re.fullmatch(r"-?\d{1,20}", raw):
                        return self._send(HTTPStatus.BAD_REQUEST, page(
                            "错误", "<p>无效群组 ID。</p>"))
                    group = render_group(db, int(raw), self.server.tz)
                    if group is not None:
                        return self._send(HTTPStatus.OK, group)
        except (sqlite3.Error, OSError, ValueError) as exc:
            # Do not render filesystem paths, SQL exception text or DB content.
            LOG.warning("面板暂时无法读取数据库（原因：%s）", type(exc).__name__)
            return self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                              page("服务暂不可用", "<p>尚未读取到机器人数据库，请检查服务状态。</p>"))
        return self._send(HTTPStatus.NOT_FOUND, page("页面不存在", "<p>404</p>"))

    def do_POST(self):
        if not self._valid_host():
            return self._send(HTTPStatus.FORBIDDEN, page("拒绝访问", "<p>无效 Host。</p>"))
        path = urlsplit(self.path).path
        if path == "/logout":
            return self._redirect(
                "/login", cookie="groupguard_session=; HttpOnly; SameSite=Strict; "
                                 "Path=/; Max-Age=0")
        if path != "/login":
            return self._send(HTTPStatus.METHOD_NOT_ALLOWED,
                              page("只读模式", "<p>不允许通过网页修改数据。</p>"))
        if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
            return self._send(HTTPStatus.BAD_REQUEST, self._login("无效的提交格式。"))
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if not 1 <= n <= 1024:
            return self._send(HTTPStatus.BAD_REQUEST, self._login("提交大小无效。"))
        request = parse_qs(self.rfile.read(n).decode("utf-8", errors="replace"))
        candidate = request.get("token", [""])[0]
        addr = self.client_address[0]
        now = time.monotonic()
        with self.server.attempt_lock:
            attempts = [t for t in self.server.failed_logins.get(addr, []) if now - t < 300]
            self.server.failed_logins[addr] = attempts
            if len(attempts) >= 5:
                return self._send(HTTPStatus.TOO_MANY_REQUESTS,
                                  self._login("尝试过于频繁，请稍后再试。"))
        if secrets.compare_digest(candidate, self.server.dashboard_token):
            with self.server.attempt_lock:
                self.server.failed_logins.pop(addr, None)
            return self._redirect(
                "/", cookie="groupguard_session=" +
                cookie_value(self.server.dashboard_token,
                             int(time.time()) + SESSION_TTL) +
                f"; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}")
        with self.server.attempt_lock:
            self.server.failed_logins.setdefault(addr, []).append(now)
        return self._send(HTTPStatus.UNAUTHORIZED, self._login("密钥无效。"))


def create_server(bind, port, token, db_path, tz):
    if not isinstance(token, str) or not 32 <= len(token) <= 256:
        raise ValueError("DASHBOARD_TOKEN 至少需要 32 个随机字符，请不要复用 Telegram Bot Token")
    server = ThreadingHTTPServer((bind, port), DashboardHandler)
    server.dashboard_token = token
    server.db_path = db_path
    server.tz = tz
    server.failed_logins = {}
    server.attempt_lock = threading.Lock()
    server.daemon_threads = True
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("DASHBOARD_TOKEN", "")
    try:
        port = int(os.environ.get("DASHBOARD_PORT", "8765"))
        server = create_server(
            os.environ.get("DASHBOARD_BIND", "127.0.0.1"), port,
            token, os.environ.get("DB_PATH", "/app/data/bot.sqlite3"),
            ZoneInfo(os.environ.get("TZ", "Asia/Shanghai")))
    except (ValueError, OSError) as exc:
        LOG.error("面板启动失败：%s", type(exc).__name__)
        raise SystemExit(2) from None
    LOG.info("面板已启动，仅供本地 SSH 隧道访问")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
