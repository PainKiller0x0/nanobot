# Core / Extensions Split

This repo is now organized so the nanobot core can track upstream cleanly.

## Principles

- `nanobot/` should stay as close to upstream as possible.
- Custom integrations should live under `extensions/`.
- Core should load extension logic only via environment configuration.

## Extension loading

- Use `NANOBOT_EXTENSION_MODULES` with comma-separated Python module names.
- Example: `extensions.reflexio`

If `NANOBOT_EXTENSION_MODULES` is empty, no extension hooks/context blocks are loaded.

## Current custom add-ons

- `extensions/reflexio`: optional retrieval + publish hook integration.
- `extensions/nanomem`: custom memory helper (external add-on sample).

## Upstream sync workflow

1. Fetch upstream and rebase/merge core branch regularly.
2. Resolve conflicts primarily in:
   - `nanobot/agent/context.py`
   - `nanobot/agent/loop.py`
   - `nanobot/agent/extensions.py`
3. Keep custom behavior in `extensions/` whenever possible.
4. Validate with quick checks:
   - `python -m py_compile nanobot/agent/context.py nanobot/agent/loop.py nanobot/agent/extensions.py`
