#!/usr/bin/env python3
"""
wx-bot-mini · pipeline.py
headless 跑一个「微信消息 -> 收件箱 -> 双投递 -> Agent 回复 -> 回写微信」场景，
落盘 output/*.json 并打印质量门报告。纯标准库，零依赖，确定性可复现。
"""
import json
import os
import sys

from core import Inbox, new_event_id

OUT = os.path.join(os.path.dirname(__file__), "output")
STORE = os.path.join(OUT, "_state.json")


def run():
    os.makedirs(OUT, exist_ok=True)
    ib = Inbox(store_path=STORE)
    ib.events = {}
    ib.outbox = []
    ib.metrics = {k: 0 for k in ib.metrics}
    ib.config = {"mode": "pull", "agent_flaky": False}

    scenario = [
        ("收：小明发来文本", {"text": "帮我把明天 9 点开会记一下", "from_wxid": "wxid_xiaoming", "from_name": "小明"}),
        ("收：小红发来语音(带转写)", {"text": "", "voice_text": "把上周的报价单发给客户", "from_wxid": "wxid_xiaohong", "from_name": "小红", "media": ["/tmp/voice_01.silk"]}),
        ("收：重复投递(同 event_id)", None),  # 占位，下面用第一条的 id 复投
        ("收：小王发来图片", {"text": "", "from_wxid": "wxid_xiaowang", "from_name": "小王", "media": ["/tmp/photo.jpg"]}),
    ]

    received = []
    # 1) 文本
    r1 = ib.receive(scenario[0][1]); received.append(r1)
    # 2) 语音
    r2 = ib.receive(scenario[1][1]); received.append(r2)
    # 3) 复投第一条 -> 去重
    dup = ib.receive({"event_id": r1["event_id"], "text": "重复一次", "from_wxid": "wxid_xiaoming"})
    # 4) 图片
    r3 = ib.receive(scenario[3][1]); received.append(r3)

    # 5) pull 投递：Agent 租赁 -> 处理 -> ack
    pulled = ib.pull(lease_ttl=300)
    for evt in pulled:
        # 模拟 Agent 处理：回一句话
        ib.send(to=evt["from_wxid"], text="（Agent）已收到并处理：「%s」" %
                (evt.get("text") or evt.get("voice_text") or "[媒体]"), binding=evt["binding"])
        ib.ack(evt["event_id"])

    # 6) webhook 模式演示：先切模式，再收一条，自动投递给内置 auto-agent
    ib.config["mode"] = "webhook"
    r4 = ib.receive({"text": "webhook 模式自动投递测试", "from_wxid": "wxid_auto"})
    # deliver_webhook 已在 receive 内触发；若因 flaky 失败则演示 requeue
    if ib.events[r4["event_id"]]["status"] == "dead":
        ib.requeue(r4["event_id"])

    # 落盘
    dump = {
        "scenario": [
            {"step": s[0], "payload": (s[1] if s[1] else {"dup_of": r1["event_id"]})}
            for s in scenario
        ],
        "received": received,
        "dedup": dup,
        "pulled_count": len(pulled),
        "events": list(ib.events.values()),
        "outbox": ib.outbox,
        "health": ib.health(),
        "gates": ib.gates(),
    }
    with open(os.path.join(OUT, "pipeline_result.json"), "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)

    # 质量门报告
    gates = ib.gates()
    passed = sum(1 for g in gates if g["pass"])
    print("=" * 56)
    print("wx-bot-mini 管线执行完成")
    print("=" * 56)
    print("接收事件：%d  | 幂等去重：%d  | 成功投递：%d  | 回写微信：%d" % (
        ib.metrics["received"], ib.metrics["deduped"], ib.metrics["delivered"], ib.metrics["sent"]))
    print("-" * 56)
    for g in gates:
        print("  [%s] %-22s %s" % (g["id"], g["name"], "PASS" if g["pass"] else "FAIL"))
    print("-" * 56)
    print("质量门：%d/%d 通过" % (passed, len(gates)))
    print("产物：output/pipeline_result.json")
    return 0 if passed == len(gates) else 1


if __name__ == "__main__":
    sys.exit(run())
