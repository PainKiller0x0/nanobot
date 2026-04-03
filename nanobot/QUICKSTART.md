# 快速上手 / Quick Start

> 本仓库由 PainKiller0x0 fork，基于 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) v0.2.0

---

## 一、安装 / Installation

```bash
# 克隆本仓库（开发模式）
git clone https://github.com/PainKiller0x0/nanobot-exp.git
cd nanobot
pip install -e .
```

> **要求：** Python 3.11+，建议使用虚拟环境：
> ```bash
> python3.11 -m venv .venv && source .venv/bin/activate
> pip install -e .
> ```

> **或者从 PyPI 安装稳定版：** `pip install nanobot-ai`

---

## 二、配置 API Key / Configure API Key

```bash
# 复制配置模板
cp nanobot.yaml.example nanobot.yaml
# 编辑 nanobot.yaml，填入你的 API key
```

`nanobot.yaml` 至少需要配置：

```yaml
# nanobot.yaml（此文件不要提交！已在 .gitignore 中）

agents:
  defaults:
    model: anthropic/claude-sonnet-4-5     # 选择你的模型

providers:
  anthropic:
    api_key: sk-ant-api03-xxxxx            # 填入你的 API key
```

> **获取 API Key：**
> - Anthropic: https://console.anthropic.com/
> - OpenAI: https://platform.openai.com/api-keys
> - DeepSeek: https://platform.deepseek.com/
> - 更多 provider 见 `nanobot.yaml.example`

---

## 三、启动（选一种渠道）/ Start a Channel

### A — QQ

nanobot 使用 QQ 官方的 [botpy](https://github.com/nonebot/qq-botpy) 库连接 QQ 机器人，无需额外的 CQHTTP 或 Lagrange。

#### 3.1 注册 QQ 开放平台账号

访问 [QQ 开放平台](https://q.qq.com/#/apps)，注册个人或企业开发者账号，只需邮箱验证和身份信息。

注册完成后，进入开发者后台，点击"创建应用" → 选择"机器人"类型，填写名称（如"AI小助手"）。

#### 3.2 获取 AppID 和 AppSecret

在应用"开发管理"页面复制两个凭证：

- **AppID**：机器人唯一标识
- **AppSecret**：API 调用密钥，妥善保管，不要泄露

#### 3.3 修改 nanobot 配置

编辑 `~/.nanobot/config.json`（或 `nanobot.yaml`）：

```json
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "你的AppID",
      "secret": "你的AppSecret",
      "allowFrom": []
    }
  }
}
```

- 将 `appId` 和 `secret` 替换为实际值
- `allowFrom: []` 留空表示允许所有用户，也可填入指定 QQ 号限制访问

#### 3.4 启动 gateway

```bash
nanobot gateway
```

正常启动后会看到：

```
[INFO] nanobot.channels.qq:on_ready - QQ bot ready: 机器人名称
```

> 注意：首次启动 QQ 机器人需要小号已加机器人好友，且机器人已在对应频道/群中。

### B — 微信（个人号，实验性）

nanobot 通过 OpenClaw 接口接入微信，**不是扫码而是点链接**。

#### 前置条件

```bash
pip install "nanobot-ai[weixin]"
```

#### 接入步骤

1. 启动后，gateway 会输出一个链接（类似 `https://xxx/wechat/connect`）
2. 用**同账号微信**的浏览器打开链接，点击获取二维码
3. 用另一个微信扫码，确认授权
4. 成功后，在微信中找到"**微信ClawBot**"聊天窗口，开始对话

```yaml
# nanobot.yaml
channels:
  weixin:
    enabled: true
```

#### ⚠️ 注意事项

- **PC 微信不可用**：截止目前，Windows 版微信尚未支持"微信ClawBot"插件，仅手机微信可用
- 授权 token 会自动保存，重启后无需重新扫码
- 建议使用小号测试，避免主号被限制

### C — Telegram

```yaml
# nanobot.yaml
channels:
  telegram:
    enabled: true
    bot_token: 123456:ABC-xxxxx   # @BotFather 获取
```

### D — CLI（无需配置渠道）

```bash
nanobot chat
```

直接在终端交互，只需配置模型，无需渠道 API key。

### E — Web/API 接口

```bash
pip install "nanobot-ai[api]"
nanobot api
# → http://localhost:18790
```

---

## 四、运行 / Launch

```bash
# 启动 gateway（所有已启用的渠道）
nanobot gateway

# 或交互式配置向导
nanobot configure
```

---

## 五、安装渠道依赖（可选）/ Optional Channel Dependencies

```bash
pip install "nanobot-ai[qq]"       # QQ 支持
pip install "nanobot-ai[weixin]"   # 微信支持
pip install "nanobot-ai[wecom]"    # 企业微信支持
pip install "nanobot-ai[matrix]"   # Matrix 支持
```

---

## 六、同步上游更新 / Sync Upstream

本仓库主要维护在 GitHub，Gitee 为镜像（自动同步）。

```bash
# 添加上游仓库（只需做一次）
git remote add upstream https://github.com/HKUDS/nanobot.git

# 拉取上游更新
git fetch upstream
git rebase upstream/main   # 或 git merge upstream/main
```

---

## 常见问题 / Troubleshooting

| 问题 | 解决 |
|------|------|
| `ModuleNotFoundError` | 重新执行 `pip install -e .` |
| 渠道无法连接 | 检查防火墙 / webhook URL |
| 模型无响应 | 确认 `nanobot.yaml` 中 API key 正确 |
| 内存占用高 | 在配置中减小 `context_window_tokens` |

更多配置选项和高级功能，见完整 [README.md](README.md)。
