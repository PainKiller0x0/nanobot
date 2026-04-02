# Quick Start — Clone & Run

This guide gets you from `git clone` to a running bot on a **fresh server** in ~5 minutes.

## 1. Clone & Install

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
pip install -e .          # dev mode (editable)
# or: pip install nanobot-ai  # from PyPI (latest stable)
```

> **Python 3.11+ required.** Use a virtual environment if you prefer:
> ```bash
> python3.11 -m venv .venv && source .venv/bin/activate
> pip install -e .
> ```

## 2. Configure API Key

```bash
# Copy the template
cp nanobot.yaml.example nanobot.yaml
```

Edit `nanobot.yaml` — only two things you must set:

```yaml
# nanobot.yaml (NEVER commit this file — it's in .gitignore)

agents:
  defaults:
    model: anthropic/claude-sonnet-4-5     # pick your model

providers:
  anthropic:
    api_key: sk-ant-api03-xxxxx            # your API key here
```

> **Where to get an API key?**
> - Anthropic: https://console.anthropic.com/
> - OpenAI: https://platform.openai.com/api-keys
> - DeepSeek: https://platform.deepseek.com/
> - See `nanobot.yaml.example` for all supported providers

## 3. Start a Channel

Pick one channel and follow the steps. Everything else can stay disabled.

### Option A — QQ (OneBot v11)

Requires a **QQ小号** + **go-cqhttp** (or Lagrange) running locally.

```yaml
# nanobot.yaml
channels:
  qq:
    enabled: true
    app_id: 123456789        # from QQ Open Platform
    secret: xxxxxx           # from QQ Open Platform
    ws_url: ws://127.0.0.1:8080/ws
```

See [QQ Channel Guide](https://github.com/HKUDS/nanobot#-qq) for how to set up go-cqhttp.

### Option B — WeChat (Personal, Experimental)

```bash
pip install "nanobot-ai[weixin]"
```

```yaml
# nanobot.yaml
channels:
  weixin:
    enabled: true
```

Scan the QR code on first launch. Token is auto-saved.

### Option C — Telegram

```yaml
# nanobot.yaml
channels:
  telegram:
    enabled: true
    bot_token: 123456:ABC-xxxxx   # from @BotFather
```

### Option D — CLI (No Setup Required)

```bash
nanobot chat
```

Direct terminal interaction — no API keys for channels needed, just the model.

### Option E — Web/API Server

```bash
pip install "nanobot-ai[api]"
nanobot api
# → http://localhost:18790
```

## 4. Launch

```bash
# Start the gateway (all enabled channels)
nanobot gateway

# Or interactive setup wizard
nanobot configure
```

## 5. (Optional) Install Channel Dependencies

```bash
pip install "nanobot-ai[qq]"    # QQ support
pip install "nanobot-ai[weixin]" # WeChat support
pip install "nanobot-ai[wecom]" # WeChat Work support
pip install "nanobot-ai[matrix]" # Matrix support
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` | `pip install -e .` again |
| Channel not connecting | Check firewall / webhook URL |
| Model not responding | Verify API key in `nanobot.yaml` |
| Memory usage too high | Reduce `context_window_tokens` in config |

For more channels, config options, and advanced features, see the full [README.md](README.md).
