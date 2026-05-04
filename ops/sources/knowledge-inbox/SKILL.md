---
name: knowledge-inbox
description: 链接收件箱与个人决策助手。用于保存 URL、生成 Markdown 预览、判断文章是否值得读、整理待读清单。
metadata:
  nanobot:
    always: true
---

# 知识收件箱与决策助手

当用户发送链接并表示“收一下 / 存一下 / 稍后看 / 加入收件箱”，或问“这个值得看吗 / 要不要读 / 收件箱里今天先看什么 / 待读列表”时，优先使用这个 skill。

## 原则

- 默认只读或只写入本地收件箱，不发送额外推送，不改服务配置。
- 对 URL 的判断必须先运行脚本抓取真实页面，不要凭标题或记忆猜。
- 输出中文，适合 QQ 阅读：先给结论，再给依据和下一步动作。
- 不暴露本地文件中的 secret/token。
- 如果网页抓取失败，直接说明失败原因，不要编造摘要。
- 收件成功后的 QQ 回执只保留结论、摘要和理由；不要输出关键词、链接清单或本地 Markdown 路径。
- 摘要优先使用已配置的免费 LongCat-Flash-Lite；不可用时退回本地摘句。

## 常用命令

保存链接并生成 Markdown：

```bash
python3 /root/.nanobot/workspace/skills/knowledge-inbox/inbox.py capture "https://example.com/article"
```

判断一个链接是否值得看：

```bash
python3 /root/.nanobot/workspace/skills/knowledge-inbox/inbox.py decide "https://example.com/article" --question "这个值得我今天读吗"
```

查看待读列表：

```bash
python3 /root/.nanobot/workspace/skills/knowledge-inbox/inbox.py list --limit 8
```

读取某条收件箱条目：

```bash
python3 /root/.nanobot/workspace/skills/knowledge-inbox/inbox.py read "条目ID"
```

删除某条收件箱条目：

```bash
python3 /root/.nanobot/workspace/skills/knowledge-inbox/inbox.py delete "条目ID"
```

生成待读决策简报：

```bash
python3 /root/.nanobot/workspace/skills/knowledge-inbox/inbox.py brief --limit 8
```

## 触发建议

- 用户只发普通网页 URL，且不是微信/鸭哥专用文章时，可以先 capture。
- 用户问“值得看吗”“要不要读”“帮我判断”，使用 decide。
- 用户问“收件箱”“待读列表”“今天先看什么”，使用 list 或 brief。
- 用户明确要求删除某条收件箱内容时，使用 delete；只删除用户指定的唯一条目，不批量清空。
- 微信文章和鸭哥 AI 正文仍优先走 RSS sidecar 专用 skill；本 skill 只负责通用链接收件箱。
- 微信 `mp.weixin.qq.com` 链接必须尝试抓取真实正文；如果只抓到“环境异常”，直接报错，不保存空文章。
