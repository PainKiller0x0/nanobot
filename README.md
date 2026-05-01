# nanobot-exp

Personal production fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot).

> This repository is not the official nanobot homepage. It is a small, upstream-friendly runtime fork used to run a personal long-lived agent with external extensions and Rust sidecars.

## 中文说明

### 这个仓库是什么

`nanobot-exp` 是我的 nanobot 实验/线上版本。目标不是把 nanobot 改成一个越来越重的个人项目，而是把职责拆开：

- `nanobot/` 尽量保持接近上游，方便继续同步 [HKUDS/nanobot](https://github.com/HKUDS/nanobot)。
- 自己写的轮子、定时任务、RSS、行情、记忆看板、通知桥等，尽量放到 extensions 或 sidecars。
- 线上部署以小内存 VPS 为目标，优先选择 Rust sidecar 和 systemd/Podman 管理。
- 公网入口收口到一个管理/看板端口，其他服务走本机或容器内部访问。

当前本地基线：`nanobot-ai 0.1.5.post3`，Python `>=3.11`。

### 设计原则

1. **上游优先**：能不改 nanobot 本体就不改，本体只保留必要补丁。
2. **外挂优先**：个人能力通过 `scripts/install_extentions.sh`、runtime glue、sidecar 接入。
3. **低内存优先**：长期运行的任务优先从 Python cron 拆到 Rust sidecar。
4. **可回滚优先**：脚本生成 overlay/env 文件，不直接把线上状态写死进仓库。
5. **公网收口**：线上建议只暴露一个 dashboard/reverse-proxy 入口，例如 `http://<host>:8093/`。

### 当前架构

```text
                        public http
                            |
                            v
                    <host>:8093
                LOF / Sidecars dashboard
                            |
       +--------------------+--------------------+
       |                    |                    |
   /rss/ proxy        /reflexio/ proxy       /obp/ proxy
       |                    |                    |
 RSS sidecar        Reflexio sidecar        OBP failover
 127.0.0.1:8091     127.0.0.1:8081         127.0.0.1:8000

 Nanobot core runs separately and talks to internal sidecars.
 QQ / Notify / health ports stay on loopback whenever possible.
```

这个结构的重点是：nanobot 负责核心聊天/Agent 循环，sidecar 负责具体业务系统。这样上游更新时，主仓库不会被个人业务逻辑缠死。

更多架构细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

### 和上游有什么不同

- 增加了线上部署脚本和精简脚本。
- 增加了 extensions 安装胶水，方便把个人能力独立出去。
- 增加了 sidecar 化迁移文档和运行时 overlay。
- 对 QQ、WeChat、Cron、内存、容器运行做了线上使用取向的补丁。
- README 不再复刻官方介绍页，而是说明这个 fork 的定位和运维方式。

### 快速开始

克隆本仓库：

```bash
git clone git@github.com:PainKiller0x0/nanobot-exp.git
cd nanobot-exp
```

安装依赖，推荐使用 `uv`：

```bash
uv sync --all-extras
```

或者用普通 Python 环境：

```bash
python3 -m pip install -e .
```

初始化配置：

```bash
nanobot onboard
```

本地聊天：

```bash
nanobot agent
```

启动网关：

```bash
nanobot gateway
```

快速回测：

```bash
scripts/run_smoke.sh
```

### Extensions 胶水

脚本名 `install_extentions.sh` 中的 `extentions` 拼写是历史兼容保留，暂时不要改名。

安装外部扩展仓库：

```bash
scripts/install_extentions.sh \
  --repo git@github.com:YOUR_ORG/nanobot-extensions.git \
  --ref main \
  --modules extensions.example

source ~/.nanobot/extensions.env
cat ~/.nanobot/extensions.lock
```

更多说明见 [docs/EXTENSIONS_GLUE.md](docs/EXTENSIONS_GLUE.md)。

### Ops / Sidecars 快照

`ops/` 保存当前线上可复现的运维胶水：Rust sidecar 源码、systemd unit、部署脚本、服务矩阵配置和 Nanobot skill 快照。

它的用途是“让这台服务器能重新拼起来”，不是保存线上状态：

- 提交源码、脚本、unit、example 配置。
- 不提交 `target/`、日志、数据库、真实 cron 目标 ID、token、env。
- 真实配置仍然放在 `/root/.nanobot` 或服务器本地 `/root/nanobot-ops`。

常用命令：

```bash
ops/scripts/deploy-sidecar.sh --status all
ops/scripts/deploy-sidecar.sh trend
ops/bin/sidecarctl status
```

当前 sidecar 入口统一经 `http://<host>:8093/` 反代，包括 `/rss/`、`/reflexio/`、`/obp/`、`/trends/`。

### Runtime / 线上脚本

常用脚本：

| Script | Purpose |
| --- | --- |
| `scripts/run_smoke.sh` | 快速回归测试 |
| `scripts/install_extentions.sh` | 安装外部扩展并生成 env/lock |
| `scripts/apply_slim_profile.sh` | 生成低内存 compose/env overlay |
| `scripts/apply_runtime_profiles.sh` | 一次性生成 runtime overlays |
| `scripts/rollback_runtime_profiles.sh` | 回滚 runtime overlays |
| `scripts/memory_report.sh` | 查看主机/容器/进程内存 |
| `scripts/memory_budget_check.sh` | 内存预算检查，适合 cron/CI |
| `scripts/ops_quick_optimize.sh` | 安全清理临时文件和旧备份 |
| `scripts/apply_wechat_rss_rs.sh` | 迁移 WeChat/RSS sidecar 的辅助脚本 |
| `scripts/tune_legacy_nanobot_container.sh` | legacy 容器精简调优 |

更多说明见 [docs/RUNTIME_PATCH_SCRIPTS.md](docs/RUNTIME_PATCH_SCRIPTS.md)。

### Docker / Compose

本仓库保留基础 compose：

```bash
docker compose up -d nanobot-gateway
```

低内存 overlay：

```bash
scripts/apply_slim_profile.sh
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d nanobot-gateway
```

如果线上仍使用 legacy `nanobot-cage`，优先用对应脚本调优，而不是手改容器：

```bash
scripts/tune_legacy_nanobot_container.sh --apply
```

### 上游同步流程

建议把官方仓库作为 `official` remote：

```bash
git remote add official https://github.com/HKUDS/nanobot.git
git fetch official --tags
```

同步上游：

```bash
git checkout main
git fetch official --tags
git merge official/main
scripts/run_smoke.sh
git push exp main
```

如果冲突发生在个人业务逻辑里，优先考虑把它继续拆到 extension/sidecar，而不是让 `nanobot/` 越来越难合并。

### Secrets 和线上数据

不要提交这些内容：

- `~/.nanobot/config.json` 中的私密配置。
- `~/.nanobot/secrets/*.env`。
- QQ、WeChat、LLM、GitHub、云厂商 token。
- 线上日志、媒体文件、数据库、RSS 抓取缓存。

仓库只放代码、脚本、文档和可复现的部署胶水。

### 相关文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/quick-start.md](docs/quick-start.md)
- [docs/configuration.md](docs/configuration.md)
- [docs/chat-apps.md](docs/chat-apps.md)
- [docs/deployment.md](docs/deployment.md)
- [docs/EXTENSIONS_GLUE.md](docs/EXTENSIONS_GLUE.md)
- [docs/RUNTIME_PATCH_SCRIPTS.md](docs/RUNTIME_PATCH_SCRIPTS.md)

### 上游致谢

核心项目来自 [HKUDS/nanobot](https://github.com/HKUDS/nanobot)，许可证见 [LICENSE](LICENSE)。本仓库保留上游 MIT License，并在此基础上维护个人实验和线上部署相关改动。

## English Brief

`nanobot-exp` is a personal production fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot).

The goal is not to turn the upstream core into a private monolith. The goal is to keep the core close to upstream, while moving personal automations into extensions and sidecars.

### What this fork adds

- Upstream-friendly runtime patches.
- Extension installer glue via `scripts/install_extentions.sh`.
- Low-memory deployment helpers.
- Sidecar-first architecture for RSS, LOF/QDII, notifications, Reflexio memory dashboard, QQ bridge and failover services.
- A single-public-entry deployment pattern, usually `http://<host>:8093/`.

### Local quick start

```bash
git clone git@github.com:PainKiller0x0/nanobot-exp.git
cd nanobot-exp
uv sync --all-extras
nanobot onboard
nanobot agent
```

Run smoke tests:

```bash
scripts/run_smoke.sh
```

### Ops / Sidecar Snapshot

The `ops/` directory tracks reproducible live-server glue: Rust sidecar sources, systemd units, deployment scripts, sidecar registry and Nanobot skill snapshots.

It intentionally excludes runtime state: build targets, logs, databases, real cron target IDs, tokens and env files.

Typical commands:

```bash
ops/scripts/deploy-sidecar.sh --status all
ops/scripts/deploy-sidecar.sh trend
ops/bin/sidecarctl status
```

### Extension install

```bash
scripts/install_extentions.sh \
  --repo git@github.com:YOUR_ORG/nanobot-extensions.git \
  --ref main
source ~/.nanobot/extensions.env
```

### Upstream sync

```bash
git fetch official --tags
git merge official/main
scripts/run_smoke.sh
git push exp main
```

Keep secrets, runtime state, logs and local data out of git.
