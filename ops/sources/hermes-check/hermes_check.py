#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
SESSION_DIR = Path('/root/.nanobot/workspace/sessions')
CRON_JOBS = Path('/root/.nanobot/workspace/cron/jobs.json')
SIDECAR_STATUS_API = os.environ.get(
    'NANOBOT_SIDECAR_STATUS_API',
    'http://127.0.0.1:8093/api/sidecars',
)
SIDECAR_LABELS = [
    ('rss', 'RSS Sidecar(8091)'),
    ('qq', 'QQ Sidecar(8092)'),
    ('lof', 'LOF Sidecar(8093)'),
    ('notify', 'Notify Sidecar(8094)'),
    ('nanobot', 'Nanobot Health(8080)'),
]
ERROR_PATTERNS = re.compile(r'(Traceback|ERROR|Exception|tool call failed|500|503|timed out|timeout|failed)', re.I)
WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def now_cn() -> datetime:
    return datetime.now(TZ)


def part_of_day(now: datetime) -> str:
    if 6 <= now.hour < 10:
        return '早间'
    if 10 <= now.hour < 14:
        return '午间'
    if 14 <= now.hour < 18:
        return '下午'
    if 18 <= now.hour < 23:
        return '晚间'
    return '夜间'


def port_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


def get_json(url: str, timeout: float = 2.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8', errors='ignore'))
    except Exception:
        return {}


def get_text(url: str, timeout: float = 2.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='ignore').strip()
    except Exception:
        return ''


def scan_errors(minutes: int = 30) -> list[str]:
    cutoff = datetime.now().timestamp() - minutes * 60
    out: list[str] = []
    if not SESSION_DIR.exists():
        return out
    for path in SESSION_DIR.rglob('*.jsonl'):
        try:
            if path.stat().st_mtime < cutoff - 300:
                continue
            with path.open('r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if ERROR_PATTERNS.search(line):
                        out.append(f'{path.name}: {line.strip()[:180]}')
                        if len(out) >= 12:
                            return out
        except Exception:
            continue
    return out


def enabled_nanobot_jobs() -> list[str]:
    try:
        data = json.loads(CRON_JOBS.read_text(encoding='utf-8'))
        return [str(j.get('id') or j.get('name') or '?') for j in data.get('jobs', []) if j.get('enabled')]
    except Exception:
        return []


def notify_summary() -> tuple[int, int, list[str]]:
    data = get_json('http://127.0.0.1:8094/api/status')
    configured = data.get('configured_jobs') or []
    states = data.get('jobs') or {}
    enabled = [j for j in configured if j.get('enabled')]
    bad: list[str] = []
    for job in enabled:
        jid = str(job.get('id') or '')
        st = states.get(jid) or {}
        if st.get('last_status') == 'error':
            bad.append(jid)
    return len(enabled), len(bad), bad[:8]


def sidecar_statuses() -> dict[str, bool]:
    """Return sidecar health using the manager API, with host-local fallbacks."""
    data = get_json(SIDECAR_STATUS_API)
    items = data.get('items') or []
    by_id = {str(item.get('id')): item for item in items if isinstance(item, dict)}
    if by_id:
        return {label: bool((by_id.get(sid) or {}).get('ok')) for sid, label in SIDECAR_LABELS}

    qq_ok = (
        get_text('http://172.17.0.1:8092/health').lower() == 'ok'
        or get_text('http://127.0.0.1:8092/health').lower() == 'ok'
    )
    return {
        'RSS Sidecar(8091)': port_ok(8091),
        'QQ Sidecar(8092)': qq_ok,
        'LOF Sidecar(8093)': port_ok(8093),
        'Notify Sidecar(8094)': port_ok(8094),
        'Nanobot Health(8080)': bool(get_json('http://127.0.0.1:8080/health')),
    }


def line_status(name: str, ok: bool) -> str:
    return f'{name}：{"✅ 正常" if ok else "❌ 异常"}'


def main() -> int:
    now = now_cn()
    errors = scan_errors(30)
    ports = sidecar_statuses()
    notify_enabled, notify_errors, notify_bad = notify_summary()
    nano_jobs = enabled_nanobot_jobs()
    weekday = WEEKDAYS[now.weekday()]

    lines = [
        '🫀 HERMES 心跳自检报告',
        f'⏰ {now:%Y-%m-%d %H:%M}（{weekday}·{part_of_day(now)}）',
        '━━━━━━━━━━━━━━━━━━━━━',
        '1️⃣ 错误扫描（近30分钟）',
        '━━━━━━━━━━━━━━━━━━━━━',
    ]
    if errors:
        lines.append(f'⚠️ 发现 {len(errors)} 条可疑日志')
        lines.extend(f'- {x}' for x in errors[:5])
    else:
        lines.append('✅ 0 个错误')

    lines.extend([
        '━━━━━━━━━━━━━━━━━━━━━',
        '2️⃣ Sidecar / 网关状态',
        '━━━━━━━━━━━━━━━━━━━━━',
    ])
    lines.extend(line_status(k, v) for k, v in ports.items())

    lines.extend([
        '━━━━━━━━━━━━━━━━━━━━━',
        '3️⃣ 定时任务状态',
        '━━━━━━━━━━━━━━━━━━━━━',
        f'Notify Sidecar：✅ {notify_enabled} 个启用任务，{notify_errors} 个错误',
        f'Nanobot Cron：✅ 仅剩 {len(nano_jobs)} 个启用任务',
    ])
    if notify_bad:
        lines.append('异常任务：' + ', '.join(notify_bad))
    if nano_jobs:
        lines.append('Nanobot保留：' + ', '.join(nano_jobs[:6]))

    lines.extend([
        '━━━━━━━━━━━━━━━━━━━━━',
        '4️⃣ 待处理',
        '━━━━━━━━━━━━━━━━━━━━━',
    ])
    if errors or notify_errors or not all(ports.values()):
        if errors:
            lines.append('⚠️ 有可疑日志，建议稍后查看 nanobot/sidecar 日志。')
        if notify_errors:
            lines.append('⚠️ Notify Sidecar 有失败任务，建议打开 8094 看详情。')
        down = [k for k, v in ports.items() if not v]
        if down:
            lines.append('⚠️ 异常服务：' + ', '.join(down))
    else:
        lines.append('✅ 暂无，系统看起来挺稳。')

    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())