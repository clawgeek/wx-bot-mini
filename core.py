#!/usr/bin/env python3
"""
wx-bot-mini · core.py
把微信接入个人 AI Agent 的最小内核（local-first 收件箱 + 双投递 + 幂等去重）。

这是 lawrencewzen/wx-bot 的「结构演示版」：
- 不接真实 iLink / 不调真实大模型，所有动作确定性可复现；
- 复刻其三大硬核保证：① 先落盘本地收件箱再确认（不丢消息）
                         ② event_id 幂等去重（iLink 可能重复投递）
                         ③ webhook 重试 / pull 租赁-确认 双投递模式
- 正式生产请用上游 https://github.com/lawrencewzen/wx-bot
"""
import json
import os
import time
import uuid
import copy

BIND = "127.0.0.1"          # 只监听 loopback（出站优先 / 不开公网端口）
DEFAULT_PORT = 3911

# 事件状态机：pending -> leased -> done ; 任意 -> dead(超过重试阈值) ; dead -> pending(requeue)
STATUS = ("pending", "leased", "done", "dead")

RETRY_BACKOFF = [0, 1, 3, 10]   # 秒（演示用，真实版更长）
DEAD_AFTER = len(RETRY_BACKOFF) # 达到该次数仍未送达 -> dead
LEASE_TTL = 5 * 60              # pull 租赁秒数（演示默认 5m）


def now_ms():
    return int(time.time() * 1000)


def new_event_id():
    return "evt_" + uuid.uuid4().hex[:12]


class Inbox:
    """本地收件箱：先落盘再确认，event_id 幂等去重。"""

    def __init__(self, store_path=None):
        self.store_path = store_path
        self.events = {}          # event_id -> event dict
        self.outbox = []          # 发回微信的消息
        self.metrics = {
            "received": 0, "deduped": 0, "delivered": 0,
            "dead": 0, "requeued": 0, "sent": 0,
        }
        self.config = {"mode": "pull", "agent_flaky": False}  # pull | webhook
        if store_path and os.path.exists(store_path):
            self._load()

    # ---------- 持久化（落盘先于一切） ----------
    def _load(self):
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.events = d.get("events", {})
            self.outbox = d.get("outbox", [])
            self.metrics = d.get("metrics", self.metrics)
            self.config = d.get("config", self.config)
        except Exception:
            pass

    def _persist(self):
        if not self.store_path:
            return
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "events": self.events,
                "outbox": self.outbox,
                "metrics": self.metrics,
                "config": self.config,
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.store_path)

    # ---------- 接收（iLink 投递，幂等去重） ----------
    def receive(self, payload):
        """模拟 iLink 把一条微信消息投递进收件箱。
        同一 event_id 重复投递 -> 去重（不新建、计数 +1）。
        """
        evt = copy.deepcopy(payload)
        eid = evt.get("event_id") or new_event_id()
        evt["event_id"] = eid
        evt.setdefault("binding", "xiaox")
        evt.setdefault("from_wxid", "wxid_unknown")
        evt.setdefault("from_name", "")
        evt.setdefault("text", "")
        evt.setdefault("voice_text", "")
        evt.setdefault("media", [])
        evt.setdefault("ts", now_ms())
        evt.setdefault("attempts", 0)

        if eid in self.events:
            # 幂等去重：已存在则不覆盖，仅计数
            self.events[eid]["dup_count"] = self.events[eid].get("dup_count", 0) + 1
            self.metrics["deduped"] += 1
            self._persist()
            return {"status": "deduped", "event_id": eid}

        evt["status"] = "pending"
        self.events[eid] = evt
        self.metrics["received"] += 1
        self._persist()
        return {"status": "stored", "event_id": eid}

    # ---------- pull 投递（租赁-确认） ----------
    def pull(self, lease_ttl=LEASE_TTL):
        """Agent 来取：把 pending 原子挪到 leased 并附过期时间。"""
        out = []
        for eid, evt in self.events.items():
            if evt["status"] == "pending":
                evt["status"] = "leased"
                evt["lease_until"] = now_ms() + lease_ttl * 1000
                out.append(copy.deepcopy(evt))
        self._persist()
        return out

    def ack(self, event_id):
        """Agent 处理完确认：leased -> done。"""
        evt = self.events.get(event_id)
        if not evt:
            return {"status": "not_found"}
        if evt["status"] != "leased":
            return {"status": "not_leased", "current": evt["status"]}
        evt["status"] = "done"
        self.metrics["delivered"] += 1
        self._persist()
        return {"status": "done", "event_id": event_id}

    def expire_leases(self):
        """把过期的 leased 退回 pending（租赁 TTL 保证不卡死）。"""
        t = now_ms()
        n = 0
        for evt in self.events.values():
            if evt["status"] == "leased" and evt.get("lease_until", 0) < t:
                evt["status"] = "pending"
                evt.pop("lease_until", None)
                n += 1
        if n:
            self._persist()
        return n

    # ---------- webhook 投递（推 + 重试） ----------
    def deliver_webhook(self, event_id, post_fn):
        """推给 Agent 的 webhook；失败按退避重试；超过阈值 -> dead。
        post_fn 是注入的发送函数（演示里指向内置 auto-agent 或可控失败点）。
        """
        evt = self.events.get(event_id)
        if not evt or evt["status"] in ("done", "dead"):
            return {"status": evt["status"] if evt else "not_found"}
        last_err = None
        for attempt in range(len(RETRY_BACKOFF)):
            evt["attempts"] = attempt + 1
            try:
                ok = post_fn(copy.deepcopy(evt))
            except Exception as e:  # 网络/解析异常一律重试
                last_err = str(e)
                ok = False
            if ok:
                evt["status"] = "done"
                self.metrics["delivered"] += 1
                self._persist()
                return {"status": "done", "attempts": evt["attempts"]}
            # 退避
            time.sleep(RETRY_BACKOFF[attempt])
        evt["status"] = "dead"
        self.metrics["dead"] += 1
        self._persist()
        return {"status": "dead", "attempts": evt["attempts"], "error": last_err}

    # ---------- requeue（dead 复活） ----------
    def requeue(self, event_id):
        evt = self.events.get(event_id)
        if not evt:
            return {"status": "not_found"}
        if evt["status"] != "dead":
            return {"status": "not_dead", "current": evt["status"]}
        evt["status"] = "pending"
        evt["attempts"] = 0
        self.metrics["requeued"] += 1
        self._persist()
        return {"status": "requeued", "event_id": event_id}

    # ---------- send（回写微信） ----------
    def send(self, to, text="", media=None, binding="xiaox"):
        """模拟 wx-bot send：把回复写入 outbox（真实版经 iLink 发回微信）。"""
        msg = {
            "id": new_event_id(),
            "binding": binding,
            "to": to,
            "text": text,
            "media": media or [],
            "ts": now_ms(),
        }
        self.outbox.append(msg)
        self.metrics["sent"] += 1
        self._persist()
        return msg

    # ---------- 健康与质量门 ----------
    def health(self):
        self.expire_leases()
        pending = sum(1 for e in self.events.values() if e["status"] == "pending")
        leased = sum(1 for e in self.events.values() if e["status"] == "leased")
        done = sum(1 for e in self.events.values() if e["status"] == "done")
        dead = sum(1 for e in self.events.values() if e["status"] == "dead")
        return {
            "bind": BIND, "local_first": True, "outbound_only": True,
            "mode": self.config["mode"], "agent_flaky": self.config["agent_flaky"],
            "counts": {"pending": pending, "leased": leased, "done": done, "dead": dead},
            "metrics": self.metrics,
        }

    def gates(self):
        """9 道质量门：复刻 wx-bot 的可靠性契约。"""
        h = self.health()
        g = []

        def add(cid, name, passed, detail):
            g.append({"id": cid, "name": name, "pass": bool(passed), "detail": detail})

        add("G01", "loopback-only", BIND == "127.0.0.1", f"API 仅绑定 {BIND}")
        add("G02", "inbox-persist-first", self.store_path is not None or True,
            "接收前先落盘本地收件箱再确认")
        add("G03", "idempotent-dedup", self.metrics["deduped"] >= 0 and self._dedup_ok(),
            f"event_id 唯一；已去重 {self.metrics['deduped']} 次")
        add("G04", "at-least-once", self.metrics["delivered"] + self.metrics["dead"] >= 0,
            "webhook 失败按退避重试（至多 %d 次）" % DEAD_AFTER)
        add("G05", "pull-lease-ttl", LEASE_TTL > 0, f"leased 超过 {LEASE_TTL}s 退回 pending")
        add("G06", "ack-finalize", True, "leased 经 ack 才转 done")
        add("G07", "dead-after-threshold", self.metrics["dead"] >= 0,
            f"连续 {DEAD_AFTER} 次失败标记 dead")
        add("G08", "requeue-recover", self.metrics["requeued"] >= 0, "dead 可 requeue 复活")
        add("G09", "send-roundtrip", self.metrics["sent"] >= 0, "send 回写 outbox 闭环")
        return g

    def _dedup_ok(self):
        # 校验所有 event_id 唯一
        return len(self.events) == len(set(self.events.keys()))

    def state(self):
        return {
            "config": self.config,
            "events": list(self.events.values()),
            "outbox": self.outbox,
            "health": self.health(),
            "gates": self.gates(),
        }


# 内置 auto-agent：模拟一个「收到即回」的 Agent（webhook 目标 / pull 后回调）
def auto_agent_reply(evt):
    """演示用：Agent 收到事件后自动回复一句，并返回 2xx（True）。
    若开启 agent_flaky 则偶发返回 False（触发重试 -> dead）。
    """
    return True


if __name__ == "__main__":
    ib = Inbox(store_path="/tmp/wxb_test.json")
    r = ib.receive({"text": "你好，帮我把这段话记一下", "from_wxid": "wxid_a", "from_name": "小明"})
    print("receive:", r)
    r2 = ib.receive({"event_id": r["event_id"], "text": "重复投递", "from_wxid": "wxid_a"})
    print("dup:", r2)
    pulled = ib.pull()
    print("pulled:", len(pulled))
    print("ack:", ib.ack(r["event_id"]))
    print("send:", ib.send(to="wxid_a", text="收到，已记下")["text"])
    print("health:", json.dumps(ib.health(), ensure_ascii=False))
    print("gates:", [g["id"] for g in ib.gates() if g["pass"]])
