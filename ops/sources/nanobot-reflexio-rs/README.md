# Nanobot-Reflexio-RS 🤖🧠

[English](#english) | [中文](#中文)

---

## English

**Nanobot-Reflexio-RS** is a high-performance memory augmentation and visualization bridge for the Nanobot Agent ecosystem. Built with Rust, it provides a robust, low-latency link between autonomous agents and persistent vector memory (Reflexio), enabling agents to "remember" contexts across sessions and visualize their cognitive state.

### Key Features
- **Nanobot Native**: Specifically optimized for the Nanobot architecture and message patterns.
- **Rust-Powered Core**: High-concurrency processing for real-time memory indexing and retrieval.
- **Reflexio Integration**: Seamlessly bridges agent logs to the Reflexio visualization dashboard.
- **Semantic Persistence**: Automatically synchronizes CLI interactions into vector embeddings via ChromaDB.
- **Cross-Session Memory**: Allows Nanobots to maintain continuity across fragmented task executions.

### Architecture
1. **Agent Hook**: Captures Nanobot's internal state and external interactions.
2. **Rust Bridge**: High-speed data pipeline for filtering and formatting logs.
3. **Reflexio Core**: Manages semantic relationships and provides the Web UI.

---

## 中文

**Nanobot-Reflexio-RS** 是专为 Nanobot Agent 生态设计的高性能记忆增强与可视化桥接器。该项目采用 Rust 编写，为自主 Agent 与持久化向量记忆系统（Reflexio）之间提供了稳定且低延迟的连接，使 Agent 能够跨会话“记住”上下文，并将其认知状态可视化。

### 核心特性
- **Nanobot 原生支持**：针对 Nanobot 的架构和消息模式进行了深度优化。
- **Rust 驱动核心**：采用高性能异步 Rust，支持实时的记忆索引与检索。
- **Reflexio 集成**：将 Agent 的运行日志无缝桥接到 Reflexio 可视化仪表盘。
- **语义持久化**：自动将 CLI 交互通过 ChromaDB 转换为向量嵌入进行存储。
- **跨会话记忆**：允许 Nanobot 在碎片化的任务执行中保持逻辑连续性。

### 系统架构
1. **Agent 钩子**：捕获 Nanobot 的内部状态与外部交互数据。
2. **Rust 桥接层**：高速数据管道，负责日志的过滤与格式化。
3. **Reflexio 核心**：管理语义关联并提供 Web 可视化界面。
