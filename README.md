# nanobot-exp

nanobot-exp is an experimental fork of nanobot.

- Upstream: https://github.com/HKUDS/nanobot
- This fork: https://github.com/PainKiller0x0/nanobot-exp

## Fork Strategy

This repository follows a split model:

1. Keep core runtime close to upstream.
2. Keep custom features in external extension repositories.
3. Connect core and extensions with a glue installer script.

That gives us faster upstream sync, smaller conflicts, and safer rollbacks.

## Quick Start

See full guide: [docs/quick-start.md](./docs/quick-start.md)

Minimal path:

```bash
git clone https://github.com/PainKiller0x0/nanobot-exp.git
cd nanobot-exp
pip install -e .
nanobot onboard
nanobot agent
```

## External Extensions (Recommended)

Use the glue installer:

```bash
scripts/install_extentions.sh \
  --repo git@github.com:YOUR_ORG/nanobot-extensions.git \
  --ref main \
  --modules extensions.reflexio

source ~/.nanobot/extensions.env
```

Then start nanobot as usual (CLI / service / docker).

## Docs

- Quick start: [docs/quick-start.md](./docs/quick-start.md)
- Extension glue: [docs/EXTENSIONS_GLUE.md](./docs/EXTENSIONS_GLUE.md)
- Upstream docs: https://nanobot.wiki/docs/latest/getting-started/nanobot-overview

## Upstream Sync

Suggested workflow:

```bash
git fetch official main
git merge official/main
# run regression tests
```

## Disclaimer

Use this fork in staging first before production rollout.
