#!/bin/bash
# Nanobot Keep-Alive Script (REPAIRED by Gemini CLI)
export ARK_SLOT_WORKSPACE=/root/.nanobot/slot_b/workspace
export PYTHONUNBUFFERED=1

echo "[$(date)] Keep-alive starting..."
while true; do
    echo "[$(date)] Starting nanobot gateway..."
    # 强制在 slot_b 下运行，因为我们手动测试时 B 是成功的
    /root/nanobot/venv/bin/python3 -m nanobot gateway --port 8080
    echo "[$(date)] Nanobot exited with code $?. Restarting in 5 seconds..."
    sleep 5
done
