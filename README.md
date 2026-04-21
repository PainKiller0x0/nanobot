# nanobot-exp

nanobot-exp is an experimental fork of nanobot.

nanobot-exp 是 nanobot 的实验性分支。

- Upstream / 上游: https://github.com/HKUDS/nanobot
- This fork / 本仓库: https://github.com/PainKiller0x0/nanobot-exp

## Fork Strategy / 分支策略

This repository follows a split model:

本仓库采用“核心与扩展分离”模式：

1. Keep core runtime close to upstream.
1. 核心运行时代码尽量贴近上游。
2. Keep custom features in external extension repositories.
2. 自定义功能放在外部扩展仓库。
3. Connect core and extensions with a glue installer script.
3. 通过胶水安装脚本在部署时连接核心与扩展。

This gives us faster upstream sync, smaller merge conflicts, and safer rollbacks.

这样可以更快同步上游、减少冲突、并且更容易回滚。

## Quick Start / 快速开始

See full guide: [docs/quick-start.md](./docs/quick-start.md)

完整说明见：[docs/quick-start.md](./docs/quick-start.md)

Minimal path / 最小路径：

```bash
git clone https://github.com/PainKiller0x0/nanobot-exp.git
cd nanobot-exp
pip install -e .
nanobot onboard
nanobot agent
```

## External Extensions (Recommended) / 外挂扩展（推荐）

Use the glue installer and pin by tag/commit:

使用胶水安装脚本，并用 tag/commit 固定版本：

```bash
scripts/install_extentions.sh \
  --repo git@github.com:YOUR_ORG/nanobot-extensions.git \
  --ref v0.3.1 \
  --modules extensions.reflexio

source ~/.nanobot/extensions.env
cat ~/.nanobot/extensions.lock
```

Then start nanobot as usual (CLI/service/docker).

然后按常规方式启动 nanobot（CLI / service / docker）。

## Fast Regression Check / 快速回归检查

Run the minimal smoke suite before push/deploy:

在 push / 部署前运行最小冒烟回归：

```bash
scripts/run_smoke.sh
```

## Docs / 文档

- Quick start / 快速开始: [docs/quick-start.md](./docs/quick-start.md)
- Extension glue / 扩展胶水安装: [docs/EXTENSIONS_GLUE.md](./docs/EXTENSIONS_GLUE.md)
- Runtime patch scripts / 运行时补丁脚本: [docs/RUNTIME_PATCH_SCRIPTS.md](./docs/RUNTIME_PATCH_SCRIPTS.md)
- Memory budget check / 内存预算检查: `scripts/memory_budget_check.sh --threshold-mib 500`
- Upstream docs / 上游文档: https://nanobot.wiki/docs/latest/getting-started/nanobot-overview

## Upstream Sync / 上游同步

Suggested workflow / 建议流程：

```bash
git fetch official main
git merge official/main
scripts/run_smoke.sh
```

## Disclaimer / 说明

Use this fork in staging first before production rollout.

建议先在预发布环境验证，再进入生产。