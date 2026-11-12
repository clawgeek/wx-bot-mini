# wx-bot-mini · Skills

把**微信**接入你的个人 AI Agent（Grok / Muse / Claude 等）的最小可跑通演示。
上游完整版见 [lawrencewzen/wx-bot](https://github.com/lawrencewzen/wx-bot)（TypeScript + Bun，MIT）。

> 本仓库是**结构演示版**：不接真实 iLink、不调真实大模型，所有动作确定性可复现，用来讲清楚「local-first 收件箱 + 双投递 + 幂等去重」这套可靠性架构。

---

## 它解决什么问题

你想让 Agent 替你管微信，但有两个硬约束：

1. **不能丢消息** —— 微信消息是实时流，任何一次网络抖动都不能让你"错过一条"。
2. **不能把本地端口暴露到公网** —— Agent 跑在云上，微信走腾讯官方通道，本地只该出站。

wx-bot 的解法（也是本 demo 复刻的核心）：

```
WeChat ──iLink──▶ wx-bot(inbox) ──webhook / pull──▶ Agent
       ◀─────────────────────────── wx-bot send ────┘
```

- **先落盘，再确认**：消息进本地收件箱（SQLite / 本 demo 用 JSON 文件）后才向 iLink 回 ACK → 不丢。
- **幂等去重**：同一 `event_id` 可能重复投递，按 id 去重，避免重复处理。
- **两种投递**：`webhook`（推，失败退避重试）或 `pull`（Agent 租赁-确认），适配不同 Agent 形态。
- **只出站**：本地 API 仅监听 `127.0.0.1`，不开公网端口。

---

## 三个能力（Skill）

### Skill 1 · 收（inbox）
每条微信消息是一个 JSON 事件：

```json
{
  "event_id": "evt_xxxx",
  "binding": "xiaox",
  "from_wxid": "wxid_xxx",
  "from_name": "昵称",
  "text": "你好",
  "voice_text": "语音转写（如有）",
  "media": ["/path/file.jpg"],
  "ts": 1794440000000
}
```

- `POST /api/receive` —— 模拟 iLink 投递一条事件到收件箱（落盘优先）。
- 重复 `event_id` 自动去重（`deduped` 计数 +1，不新建）。

### Skill 2 · 投递（delivery）
- **webhook**：`POST` 事件给 Agent 的 URL，返回 2xx 即送达；失败按 `0/1/3/10s` 退避重试，超过 4 次标记 `dead`。
- **pull**：`GET /api/inbox/pull?lease=300` 租赁领取（状态 `leased`，带 TTL），处理完 `POST /api/inbox/ack` 确认（转 `done`）；过期自动退回 `pending`。
- `POST /api/inbox/requeue` —— `dead` 事件复活回 `pending`。

### Skill 3 · 回（send）
- `POST /api/send` —— `{to, text, media}` 把回复写回 outbox（真实版经 iLink 发回微信）。
- 微信不渲染 Markdown，只发纯文本。

---

## 质量门（可靠性契约）

| ID | 名称 | 含义 |
|----|------|------|
| G01 | loopback-only | API 仅绑 127.0.0.1，不开公网 |
| G02 | inbox-persist-first | 接收前先落盘再确认 |
| G03 | idempotent-dedup | event_id 唯一、重复去重 |
| G04 | at-least-once | webhook 失败退避重试 |
| G05 | pull-lease-ttl | leased 超 TTL 退回 pending |
| G06 | ack-finalize | leased 经 ack 才转 done |
| G07 | dead-after-threshold | 连续 4 次失败标记 dead |
| G08 | requeue-recover | dead 可 requeue 复活 |
| G09 | send-roundtrip | send 回写 outbox 闭环 |

---

## 本地跑

```bash
python3 server.py 3911          # 启动本地 API（仅 127.0.0.1）
# 另开终端：
python3 pipeline.py             # 跑场景，落 output/*.json，打印质量门
```

浏览器打开 `index.html`：自动检测本地 API（在线 `[live]`）或降级内置 JS 引擎（`[mock]`）。
深链：`index.html?tab=inbox&run=1` 可一键演示。

> 真实接入微信请用上游仓库（需腾讯官方 ClawBot / iLink 资质，且只能本人使用）。
