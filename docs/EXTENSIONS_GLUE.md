# Extensions Glue Install

Keep `nanobot` core clean and install custom extensions at deployment time.

## Script

Use either name (both point to the same installer):

```bash
scripts/install_extentions.sh
scripts/install_extensions.sh
```

## Example

```bash
scripts/install_extentions.sh \
  --repo git@github.com:YOUR_ORG/nanobot-extensions.git \
  --ref main \
  --modules extensions.reflexio

source ~/.nanobot/extensions.env
```

## What it does

1. Pulls extension source into `~/.nanobot/extensions-runtime/src`.
2. If the repo is a Python package, runs editable install.
3. Generates `~/.nanobot/extensions.env` with runtime exports:

- `PYTHONPATH` to include the extension source.
- `NANOBOT_PROVIDER_FAILOVER_MODULE` (auto if `extensions/provider_failover` exists).
- `NANOBOT_EXTENSION_MODULES` (when `--modules` is provided).

## Service / Docker usage

- Systemd/shell startup: `source ~/.nanobot/extensions.env` before running nanobot.
- Docker Compose: add this file as `env_file` or copy exports into `environment`.
