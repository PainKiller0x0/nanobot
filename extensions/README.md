# Extensions

This directory contains optional add-ons that are intentionally kept outside nanobot core.

## Enable extensions

Set `NANOBOT_EXTENSION_MODULES` to a comma-separated module list.

Example:

```bash
export NANOBOT_EXTENSION_MODULES=extensions.reflexio
```

When unset, nanobot core runs without loading any external extension logic.
