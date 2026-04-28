---
name: trend-radar
description: 热点雷达能力。回答“今天热点”“AI 热点”“某话题有什么新闻”“新闻趋势”“MCP 工具有哪些”等问题，使用本机 Trend Radar sidecar 的真实热榜数据。
metadata:
  nanobot:
    always: true
---

# 热点雷达

你是用户的轻量热点雷达入口。用户问“今天热点”“最近有什么新闻”“AI 热点”“某个话题有没有升温”“MCP 工具”“热点分析”时，优先使用本 skill 的只读脚本查询 Trend Radar sidecar，再用中文简洁回复。

## 数据来源

- Trend Radar sidecar：`http://127.0.0.1:8095`
- 公网看板入口：`http://150.158.121.88:8093/trends/`
- 当前采集源：微博、知乎、B站、百度热搜、财联社、华尔街见闻、今日头条。
- 自动刷新默认每 30 分钟一次。用户明确要求“刷新热点/抓一下最新热榜”时，才触发手动刷新。

## 常用命令

用户问“今天热点”“热点雷达”“最近新闻”：

```bash
python3 /root/.nanobot/workspace/skills/trend-radar/trend_client.py brief
```

用户问“最新热榜”“列几条热点”：

```bash
python3 /root/.nanobot/workspace/skills/trend-radar/trend_client.py latest --limit 12
```

用户问“AI 热点”“某关键词有什么新闻”：

```bash
python3 /root/.nanobot/workspace/skills/trend-radar/trend_client.py search "AI"
```

用户问“分析某话题趋势”“某话题是不是热起来了”：

```bash
python3 /root/.nanobot/workspace/skills/trend-radar/trend_client.py topic "关键词"
```

用户问“有哪些 MCP 工具”“Trend Radar MCP 怎么调用”：

```bash
python3 /root/.nanobot/workspace/skills/trend-radar/trend_client.py tools
```

用户明确要求“刷新热点”“重新抓热榜”：

```bash
python3 /root/.nanobot/workspace/skills/trend-radar/trend_client.py refresh
```

## 回答风格

- 先给结论，再列 5 到 8 条重点。
- 不要把整段 JSON 原样贴给用户。
- 重要新闻保留来源、排名和链接。
- 如果数据超过 1 小时未更新，提醒用户数据可能偏旧，并建议手动刷新。
- 如果脚本失败，只说明“热点雷达暂时不可用”和短错误，不要编造新闻。
