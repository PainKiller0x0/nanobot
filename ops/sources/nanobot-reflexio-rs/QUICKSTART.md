# Quick Start / 快速入门

[English](#english) | [中文](#中文)

---

## English

### 1. Installation
Ensure you have the Rust toolchain installed.
```bash
git clone https://github.com/PainKiller0x0/nanobot-reflexio-rs.git
cd nanobot-reflexio-rs
cargo build --release
```

### 2. Configuration
Copy the template configuration and update your environment settings (ChromaDB URL, Agent ID, etc.):
```bash
cp .env.example .env
# Edit .env with your specific Reflexio endpoint
```

### 3. Running the Bridge
Start the Nanobot memory bridge in the background:
```bash
# Execute the compiled binary
./target/release/nanobot-reflexio-bridge
```

### 4. Viewing Memories
Open your Reflexio dashboard URL (e.g., your configured server IP) to see the Nanobot's live memory stream.

---

## 中文

### 1. 安装步骤
请确保已安装 Rust 工具链。
```bash
git clone https://github.com/PainKiller0x0/nanobot-reflexio-rs.git
cd nanobot-reflexio-rs
cargo build --release
```

### 2. 配置说明
复制模板配置文件并更新环境变量（如 ChromaDB 地址、Agent ID 等）：
```bash
cp .env.example .env
# 根据你的 Reflexio 服务地址编辑 .env 文件
```

### 3. 运行桥接器
在后台启动 Nanobot 记忆桥接器：
```bash
# 执行编译后的二进制文件
./target/release/nanobot-reflexio-bridge
```

### 4. 查看记忆
打开你的 Reflexio 仪表盘地址（例如你配置的服务器 IP），即可实时查看 Nanobot 的记忆流。
