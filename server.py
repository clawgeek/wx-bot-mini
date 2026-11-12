#!/usr/bin/env python3
"""
wx-bot-mini · server.py
本地优先的微信<->Agent 桥接 API（演示版，绑 127.0.0.1 loopback，免 key）。

复刻 lawrencewzen/wx-bot 的本地 API 契约：
  GET  /api/health                  -> 健康（bind/模式/计数）
  POST /api/receive                 -> 模拟 iLink 投递一条微信事件到收件箱
  GET  /api/inbox/pull?lease=300    -> Agent 租赁领取 pending 事件
  POST /api/inbox/ack               -> {event_id} 确认完成
  POST /api/inbox/requeue           -> {event_id} dead 复活
  POST /api/send                    -> {to,text,media} 回写微信（发到 outbox）
  POST /api/deliver/webhook         -> 对某事件跑 webhook 投递（内置 auto-agent）
  POST /api/config                  -> {mode,agent_flaky} 切换投递模式/失败注入
  GET  /api/state                   -> 全量状态（前端仪表盘）
  POST /api/reset                   -> 清空（演示用）

启动： python3 server.py [port]   默认 3911
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core import Inbox, BIND, auto_agent_reply

STORE = "/tmp/wx-bot-mini-state.json"
ROOT = os.path.dirname(os.path.abspath(__file__))
_lock = threading.Lock()
INBOX = Inbox(store_path=STORE)


def _json_body(handler):
    try:
        ln = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(ln) if ln else b"{}"
        return json.loads(raw or b"{}")
    except Exception:
        return {}


def _post_to_agent(evt):
    """webhook 投递目标：演示里指向内置 auto-agent。
    若开启 agent_flaky，偶发失败以演示 重试->dead->requeue。
    """
    if INBOX.config.get("agent_flaky") and (evt.get("attempts", 0) % 2 == 1):
        return False
    # 内置 Agent：收到即回一句，并回写微信（模拟 send）
    reply = "（auto-agent）收到：「%s」已处理。" % (evt.get("text") or evt.get("voice_text") or "[媒体]")
    INBOX.send(to=evt.get("from_wxid", "wxid_unknown"), text=reply, binding=evt.get("binding", "xiaox"))
    return auto_agent_reply(evt)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        with _lock:
            if u.path == "/api/health":
                return self._send(200, INBOX.health())
            if u.path == "/api/state":
                return self._send(200, INBOX.state())
            if u.path == "/api/inbox/pull":
                qs = parse_qs(u.query)
                lease = int(qs.get("lease", ["300"])[0])
                evts = INBOX.pull(lease_ttl=lease)
                return self._send(200, {"leased": len(evts), "events": evts})
        # 静态文件服务（与 API 同源，便于前端 fetch("/api/...") 命中）
        return self._serve_static(u.path)

    def _serve_static(self, path):
        rel = path.lstrip("/")
        if rel in ("", "index.html"):
            rel = "index.html"
        fpath = os.path.normpath(os.path.join(ROOT, rel))
        if not fpath.startswith(ROOT) or not os.path.isfile(fpath):
            return self._send(404, {"error": "not_found", "path": path})
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png", ".svg": "image/svg+xml", ".md": "text/markdown; charset=utf-8",
        }.get(os.path.splitext(fpath)[1], "application/octet-stream")
        try:
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self._send(404, {"error": "read_failed"})

    def do_POST(self):
        u = urlparse(self.path)
        b = _json_body(self)
        with _lock:
            if u.path == "/api/receive":
                r = INBOX.receive(b)
                # webhook 模式：收到即尝试投递
                if INBOX.config["mode"] == "webhook" and r.get("status") == "stored":
                    INBOX.deliver_webhook(r["event_id"], _post_to_agent)
                return self._send(200, r)
            if u.path == "/api/inbox/ack":
                return self._send(200, INBOX.ack(b.get("event_id")))
            if u.path == "/api/inbox/requeue":
                return self._send(200, INBOX.requeue(b.get("event_id")))
            if u.path == "/api/send":
                m = INBOX.send(to=b.get("to", ""), text=b.get("text", ""),
                               media=b.get("media"), binding=b.get("binding", "xiaox"))
                return self._send(200, m)
            if u.path == "/api/deliver/webhook":
                return self._send(200, INBOX.deliver_webhook(b.get("event_id"), _post_to_agent))
            if u.path == "/api/config":
                if "mode" in b:
                    INBOX.config["mode"] = b["mode"]
                if "agent_flaky" in b:
                    INBOX.config["agent_flaky"] = bool(b["agent_flaky"])
                INBOX._persist()
                return self._send(200, {"config": INBOX.config})
            if u.path == "/api/reset":
                INBOX.events = {}
                INBOX.outbox = []
                INBOX.metrics = {k: 0 for k in INBOX.metrics}
                INBOX._persist()
                return self._send(200, {"reset": True})
        return self._send(404, {"error": "not_found", "path": u.path})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3911
    srv = ThreadingHTTPServer((BIND, port), H)
    print("wx-bot-mini local API on http://%s:%d  (loopback only)" % (BIND, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
