# nanobot-exp 架构设计

本文档描述 `nanobot-exp` 当前线上实现。它不是愿景文档，而是为了后续改动时能快速判断边界、风险和回滚方式。

## 目标

`nanobot-exp` 是基于上游 `HKUDS/nanobot` 的个人线上 fork。核心规则是：

> Nanobot 本体尽量贴近上游，个人产品、定时任务、看板和集成尽量放到 sidecar 或 skill。

架构目标：

- `nanobot/` 保持小而清晰，方便继续合并上游。
- 长期运行的个人业务逻辑不塞进 Python core。
- 小内存 VPS 上优先使用 Rust sidecar。
- 公网只保留一个 Web 入口。
- secret、日志、数据库、真实 cron 目标和运行态数据不进 Git。
- 每个服务都能独立重启、独立回测、独立回滚。

## 运行拓扑

```text
                         public internet
                              |
                              v
                    http://<host>:8093
                  LOF dashboard + reverse proxy
                              |
       +----------------------+----------------------+
       |          |           |          |           |
     /rss/    /reflexio/    /obp/    /trends/   /sidecars
       |          |           |          |           |
  RSS sidecar  Reflexio   OBP bridge  Trend     manager API
  127.0.0.1    127.0.0.1  127.0.0.1   Radar     same process
  :8091        :8081      :8000       :8095

  Nanobot core 和内部桥接不直接暴露公网：

  nanobot-cage          127.0.0.1:8080    Podman
  qq-sidecar-rs         172.17.0.1:8092   systemd, Podman bridge 可访问
  notify-sidecar-rs     127.0.0.1:8094    systemd
```

健康检查约定：

- 服务健康以 `8093/api/sidecars` 聚合结果为准，不再让脚本各自猜端口。
- QQ Sidecar 线上通过 `20-podman-bridge.conf` 绑定 `172.17.0.1:8092`，不是 `127.0.0.1:8092`。
- HERMES 自检必须读 sidecar manager API，避免端口绑定变化造成误报。

只有 `lof-sidecar-rs` 作为公网入口。它负责：

- `/` 和 `/lof`：LOF/QDII 看板。
- `/sidecars` 和 `/api/sidecars`：服务矩阵。
- `/rss/`、`/reflexio/`、`/obp/`、`/trends/`：反代到内部 sidecar。

`podman-port-forward-allow.service` 是公网端口守卫。预期端口策略是：

- 公网开放：`8093`，以及 SSH 和云厂商必要管理端口。
- 仅本机或容器桥接：`8000`、`8080`、`8081`、`8091`、`8092`、`8094`、`8095`。

## 服务矩阵

服务注册表以 `ops/config/sidecars.json` 为准。线上运行副本在 `/root/.nanobot/sidecars.json`。

| ID | 服务 | 运行方式 | 端口 | 公网路径 | 职责 |
|---|---|---:|---:|---|---|
| `nanobot` | Nanobot Core | Podman | `8080` | 无 | QQ/WeChat 入口、agent loop、dream |
| `rss` | RSS Sidecar | Podman | `8091` | `/rss/` | 微信文章、鸭哥 AI、Markdown 预览、广告过滤 |
| `qq` | QQ Bridge | systemd | `8092` | 无 | QQ API 直连探测、签名发送支持 |
| `lof` | LOF Dashboard | systemd | `8093` | `/` | QDII/LOF 看板、公网反代、服务总控 |
| `notify` | Notify Bridge | systemd | `8094` | 无 | cron 调度、重试状态、QQ 通知分发 |
| `trend` | Trend Radar | systemd | `8095` | `/trends/` | NewsNow 热榜、搜索、话题分析、MCP 风格工具 |
| `reflexio` | Reflexio | systemd | `8081` | `/reflexio/` | 记忆和反思看板 |
| `obp` | OBP Bridge | systemd | `8000` | `/obp/` | 模型网关、双协议 API、成本和兜底控制台 |
| `podman-public-rule` | Port Guard | systemd | n/a | 无 | 阻断旧业务端口公网访问 |

## 目录布局

```text
nanobot-exp/
  nanobot/                         上游核心，尽量少改
  scripts/                         上游友好的运行时补丁脚本
  docs/                            用户文档和架构文档
  ops/
    config/sidecars.json           脱敏后的服务注册表
    config/notify-sidecar-rs/
      config.example.json          示例配置，不含真实 QQ 目标 ID
    bin/sidecarctl                 日常运维 CLI
    scripts/deploy-sidecar.sh      构建、安装、重启、状态检查入口
    scripts/check-nanobot-exp-patches.sh
                                    上游同步后检查 exp 必保留补丁
    sbin/                          主机辅助脚本
    systemd/                       systemd unit 和 drop-in
    sources/
      _shared/                     skill 客户端共享 Python helper
      hermes-check/                HERMES 自检脚本快照
      qdii-monitor/                LOF notify 包装脚本快照
      *-rs/                        Rust sidecar 源码快照
      *-assistant/                 Nanobot skill 源码快照
```

线上还有 `/root/nanobot-ops`，这是实际运维工作副本，`/usr/local/sbin/deploy-sidecar` 默认从这里构建和部署。
GitHub 里的 `ops/` 是它的脱敏快照。

## 部署模型

部署链路保持简单：

```text
修改 /root/nanobot-ops 源码
        |
        v
deploy-sidecar <target>
        |
        +-- Rust sidecar: cargo build --release + install 到 /usr/local/bin
        +-- RSS sidecar: podman build + restart local image
        |
        v
systemd restart + health check
```

常用命令：

```bash
deploy-sidecar all --status
deploy-sidecar lof
deploy-sidecar trend
sidecarctl status
sidecarctl logs lof
sidecarctl restart notify
systemctl status nanobot-stack.target
```

`nanobot-stack.target` 用轻量 `PartOf=` drop-in 把服务分组。它只是 systemd 分组，不是调度系统，也不是 k8s。

## 数据和状态归属

边界必须清楚：

- Git 保存代码、文档、示例配置、systemd unit 和部署胶水。
- `/root/.nanobot` 保存运行配置、workspace skills、sidecar 状态、RSS 数据库和 secrets。
- `/root/.nanobot/secrets/*.env` 保存凭据和代理认证材料。
- Rust `target/`、日志、SQLite 数据库、真实 notify 配置不提交。

| 数据 | 归属 | 是否进 Git |
|---|---|---|
| QQ app secret | `/root/.nanobot/config.json` 或 secrets env | 不提交 |
| Notify 目标 ID | 线上 `config.json` | 不提交 |
| Notify 示例配置 | `ops/config/notify-sidecar-rs/config.example.json` | 提交 |
| Trend cache | `/root/.nanobot/data/trend-sidecar/state.json` | 不提交 |
| RSS DB | live sidecar volume/workspace | 不提交 |
| Sidecar 源码 | `ops/sources/*` | 提交 |

## Nanobot Core 边界

Nanobot core 应该负责：

- 聊天入口和出口。
- Agent loop、LLM/tool 编排。
- 很难外置的小型路由胶水。
- 调用本地 sidecar API 和 skill 脚本。

Nanobot core 不应该负责：

- RSS 抓取和文章存储。
- QDII/LOF 行情抓取。
- Cron 执行和 retry 状态。
- 热榜新闻采集。
- Web dashboard。
- 长期运行的个人业务逻辑。

如果一个功能可以表达为 `HTTP API + CLI/script + dashboard`，通常应该做成 sidecar 或 skill，而不是继续改 core。

当前仍然必须承认的 `nanobot-exp` 本体/运行时补丁：

- QQ channel：`ops/config/overrides/qq.py` 是线上 QQ 通道覆盖实现，容器启动时由 `/root/.nanobot/overrides/apply_overrides.py` 覆盖到 `/app/nanobot/channels/qq.py`。它包含签名校验、长文本发送、媒体、fast path 和 sidecar 下载等线上能力。
- Gateway heartbeat：`gateway.heartbeat.deliveryChannel` / `deliveryChatId` 用来固定原生 heartbeat 投递目标，避免“最近活跃渠道”把自省报告发到 WeChat。
- 上游同步后必须跑 `ops/scripts/check-nanobot-exp-patches.sh /root/nanobot`，至少确认 heartbeat 投递、HERMES manager check、LOF refresh-before-send 这些补丁还在。
- 若怀疑 core drift，先看 `git diff official/main...HEAD -- nanobot/`，再判断要不要把逻辑继续 sidecar 化。

## Skills 和公共 helper

个人 skill 的源码快照放在 `ops/sources/*`，线上运行副本在 workspace。

共享 Python helper：

```text
ops/sources/_shared/ops_common.py
```

当前提供：

- `JsonHttpClient`：base URL fallback、JSON GET/POST、文本请求，支持浮点秒级 timeout。
- `parse_dt`、`fmt_time`、`now_shanghai`。
- `holiday_info`、`is_cn_workday`：复用中国法定节假日/补班日判断。
- `short`：适合 QQ 输出的短文本截断。

目前应复用它的脚本包括：

- `trend-radar/trend_client.py`
- `personal-ops-assistant/ops_summary.py`
- `wechat-rss-sidecar-skill/client.py`
- `hermes-check/hermes_check.py`
- `qdii-monitor/send_qq.py`

这样 skill/ops 脚本不需要各自复制 HTTP fallback、JSON 解析、timeout 和时间解析逻辑。

抽取原则：

- 通用 IO、时间、短文本 helper 放 `_shared`。
- 业务格式化留在各自 skill。
- 不把 secret、真实目标 ID、机器私有状态塞进共享代码。

## Sidecar 职责

### `lof-sidecar-rs`

- `8093` 公网入口。
- LOF/QDII 看板、报告、历史溢价视图。
- `/api/run` 是同步刷新接口；`/api/status` 是状态和缓存读取接口。
- 内部 sidecar 反代。
- 服务矩阵和健康聚合。

LOF 定时报告不是直接读缓存发送。Notify 任务调用 `qdii-monitor/send_qq.py`，脚本会：

1. 先 POST `/api/run` 触发同步刷新。
2. 最多等待 `LOF_RUN_TIMEOUT_SECS`，默认 60 秒。
3. 成功则发送新报告。
4. 刷新失败或超时才回退当天新鲜缓存，并在输出前加 `[WARN]`。

这个顺序很重要：交易时段 5 分钟差异足够影响判断，不能优先发旧缓存。

### `wechat-rss-rs`

- RSS 订阅管理。
- 微信文章和鸭哥 AI 抓取。
- Markdown 预览。
- LLM 设置和广告过滤。
- 用 Podman 隔离 RSS 运行环境。

### `notify-sidecar-rs`

- cron-like 调度。
- retry/timeout 状态。
- 通过 QQ bridge 或 Nanobot 配置分发通知。
- 负责 HERMES、天气、RSS/鸭哥、LOF 报告等主动推送。
- 把循环任务从 Nanobot core 内存里拿出去。

HERMES 任务调用 `hermes-check/hermes_check.py`。脚本应读取 `8093/api/sidecars` 聚合健康状态，而不是硬编码逐个端口探测。

### `trend-sidecar-rs`

- NewsNow 热榜采集。
- 本地缓存和自动刷新。
- 搜索、话题分析、摘要 API。
- `/trends/mcp` 下提供 MCP 风格 JSON-RPC 工具。

### `qq-sidecar-rs`

- 轻量 QQ API 桥。
- 直连发送健康探测。
- 给 notify 脚本提供稳定本地目标。

### `nanobot-reflexio-rs`

- Reflexio 风格记忆/反思看板。
- 有独立 Web 和数据生命周期，所以不放 core。

### `obp-rs`

- 模型网关、成本统计、fallback 控制台。
- 公网访问必须经 `8093/obp`，并保留认证或网络限制。
- 外部 OpenAI 兼容入口：`/obp/v1/chat/completions`。
- 外部 Anthropic 兼容入口：`/obp/anthropic/v1/messages`。
- client API 协议和 upstream 渠道协议解耦：同一个外部请求可以路由到 OpenAI-compatible 或 Anthropic 渠道；非流式响应会做基础格式转换。
- 流式请求只做同协议透传；跨协议 streaming 暂不转换，避免引入额外延迟和不完整 SSE 语义。
- 每次响应带 `x-obp-route`、`x-obp-group`、`x-obp-requested-model`、`x-obp-actual-model`、`x-obp-channel`、`x-obp-reason`。Nanobot provider 只在 OBP endpoint 上读取这些头并写入 `podman logs -f nanobot-cage`。

路由策略原则：

- 不用“累计 token / 字符数 / 消息数”自动升 Pro。长度只能说明成本压力，不能说明任务难度。
- 显式请求 `pro_model`、`backup_model`、`emergency_model` 时优先尊重；月预算硬熔断除外。
- 默认模型保持 smart-routable：普通闲聊、状态查询、天气、cron、LOF/RSS 这类轻任务走默认模型。
- 上下文压缩、记忆整理、反思、架构/review/排障等复杂任务才走 Pro。
- 关键词只看当前最后一条用户消息，不扫描完整历史，避免历史里的“深度/架构/review”让普通闲聊误升 Pro。
- `x-obp-purpose` / `x-obp-intent` 或请求体 `metadata.purpose` / `metadata.intent` 可作为零额外延迟 hint；不额外调用分类模型。
- 月预算降级只抑制自动升 Pro，不影响普通默认模型请求。
- 月预算硬熔断优先走 backup；backup 失败后才走 emergency。日常主模型超时或上游报错时，fallback 顺序仍优先 emergency，保证体验。

## MCP 和 AI 分析路径

当前 MCP-like 路径优先本地化：

```text
Trend Radar sidecar
  /api/trends/*
  /api/mcp/tools
  /mcp
        |
        v
Nanobot skill 或 LLM call
        |
        v
QQ 回复 / dashboard 摘要
```

这样可以先获得 MCP 能力形态，又不引入重型 MCP server stack。
如果未来需要外部 MCP client，再优先加认证和内网监听，不要直接裸露公网。

## 当前实现 review

方向是对的：

- core/sidecar 拆分已经形成，个人功能大多离开 `nanobot/`。
- Podman 迁移后，常驻内存比 Docker 低。
- 服务矩阵和 `sidecarctl` 让 health/log/restart 有统一入口。
- Trend Radar 提供新闻采集和 MCP 风格工具，但没有把重 Python 服务塞进 core。
- `_shared/ops_common.py` 已经减少 skill 客户端重复代码。
- 最近一次实现 review 已将 HERMES 和 LOF notify wrapper 的 HTTP/JSON/timeout 逻辑收口到 `JsonHttpClient`，去掉了 LOF wrapper 对 `requests` 的依赖。

主要技术债：

- `lof-sidecar-rs` 仍然偏大，一个文件里同时有 dashboard、LOF 逻辑、反代和服务管理。
- `wechat-rss-rs` 偏大，UI、settings、crawler、DB、LLM test endpoint 混在一起。
- `ops/` 快照和 `/root/nanobot-ops` 线上工作副本可能漂移，需要把 sync/commit 变成习惯。
- `/obp/` 和未来 MCP 入口的认证边界要继续显式维护，不能为了方便把 admin 面裸露出去。
- 部分 systemd unit 指向 `/root/.nanobot` 线上路径，这是设计选择，但恢复环境时必须先恢复 workspace 和 secrets。

建议下一步重构：

1. Rust sidecar 的大块 HTML/CSS 如果继续增长，拆到 `static` 或 `include_str!` 文件。
2. `lof-sidecar-rs` 如果继续加功能，把 reverse proxy、service manager、LOF domain 分 module。
3. `wechat-rss-rs` 拆 DB、settings、crawler、LLM client 模块。
4. 增加 `ops/scripts/check-architecture.sh`，检查 sidecars registry、systemd unit、文档端口是否一致。
5. 新个人自动化默认采用 `skill + sidecar API`，除非确实必须改 Nanobot core。

## 上游同步 checklist

同步 `HKUDS/nanobot` 后先做这些检查：

```bash
git diff official/main...HEAD -- nanobot/
ops/scripts/check-nanobot-exp-patches.sh /root/nanobot
PYTHONPATH=/root/nanobot uv run pytest tests/cli/test_commands.py::test_heartbeat_delivery_target_config_aliases
```

注意：服务器上可能装有系统级旧 `nanobot` 包；本地回测必须加 `PYTHONPATH=/root/nanobot`，否则 pytest 可能导入 `/usr/local/lib/python.../dist-packages/nanobot` 而不是当前源码。

如果 `check-nanobot-exp-patches.sh` 失败，先不要重启线上服务，先确认是上游重构导致的真实冲突，还是 ops 快照没有同步。

## 模型切换 smoke

切换默认模型、Pro 模型、backup/emergency 模型或 OBP upstream 渠道后，必须跑模型切换 smoke：

```bash
python3 ops/scripts/smoke-model-switch.py --with-llm
python3 ops/scripts/smoke-model-switch.py --refresh-lof
```

覆盖范围：

- sidecar manager、dashboard system、LOF 状态和刷新。
- RSS 订阅、最近文章、Markdown 预览、自动刷新。
- Notify cron jobs、QQ sidecar、Reflexio stats。
- Trend Radar 状态、MCP 工具列表和 MCP-like call。
- OBP OpenAI shell、Anthropic shell、工具调用、compact 升 Pro、历史关键词防污染。
- Nanobot provider 默认模型链路和 `podman logs -f nanobot-cage` 的 OBP route header。

这套 smoke 的目标不是测模型智商，而是确认“换模型不丢 sidecar 能力、不误升 Pro、不破坏工具调用、不把预算熔断顺序打乱”。

## 变更 checklist

新增功能时按这个顺序判断：

1. 先定边界：core、skill、sidecar、script。
2. 如果长期运行或拥有数据，优先 sidecar。
3. 受管理服务必须写入 `sidecars.json`。
4. 增加 health endpoint 和 `deploy-sidecar` 支持。
5. secret 和 live data 不进 Git。
6. sidecar API 稳定后，再加 skill 或 QQ fast path。
7. 回测：

```bash
deploy-sidecar <target>
deploy-sidecar all --status
python3 -m py_compile <changed-python-scripts>
cargo check --offline --manifest-path <changed-rust-sidecar>/Cargo.toml
ops/scripts/check-nanobot-exp-patches.sh /root/nanobot
```

8. 只要服务图、core patch、端口绑定、主动推送链路或共享 helper 边界变化，就同步更新本文档。
