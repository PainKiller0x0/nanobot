# Quick Start (nanobot-exp)

This page provides the shortest usable setup for nanobot-exp.

## 1. Install

```bash
git clone https://github.com/PainKiller0x0/nanobot-exp.git
cd nanobot-exp
pip install -e .
```

## 2. Initialize Config

```bash
nanobot onboard
```

Default config path:

- Linux/macOS: `~/.nanobot/config.json`

## 3. Configure Model and API Key

Edit `~/.nanobot/config.json` with at least:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

## 4. Start Chat

```bash
nanobot agent
```

## 5. Install External Extensions (Optional)

nanobot-exp recommends keeping custom features in a separate extension repo:

```bash
scripts/install_extentions.sh \
  --repo git@github.com:YOUR_ORG/nanobot-extensions.git \
  --ref main \
  --modules extensions.reflexio

source ~/.nanobot/extensions.env
```

Then start nanobot again.

## 6. Useful Commands

```bash
nanobot --help
nanobot channels --help
nanobot gateway --help
```

## 7. Troubleshooting

- Config error: validate JSON format in `~/.nanobot/config.json`.
- Provider error: verify API key and provider/model match.
- Extension not loaded: confirm `source ~/.nanobot/extensions.env` and check `PYTHONPATH` and `NANOBOT_EXTENSION_MODULES`.

More extension details: [EXTENSIONS_GLUE.md](./EXTENSIONS_GLUE.md)
