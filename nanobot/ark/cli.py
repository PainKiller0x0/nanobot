"""ARK CLI commands."""
import asyncio
import json
from pathlib import Path

import typer

from loguru import logger

from .slot_manager import SlotManager, PID_FILE, PENDING_SWITCH_FILE
from .ark import ArkOrchestrator

app = typer.Typer(help="NanoBot ARK — 三层容灾管理", no_args_is_help=True)


def _log_setup():
    """配置日志输出到 stderr"""
    import sys
    logger.remove()
    logger.add(sys.stderr, format="<level>{message}</level>", level="INFO")


@app.command()
def status():
    """查看 ARK 槽位状态"""
    _log_setup()
    sm = SlotManager()
    st = sm.status()

    def c(cond, yes, no=""):
        return f"[green]{yes}[/green]" if cond else (f"[red]{no}[/red]" if no else "—")

    print(f"Current slot:  {st['current_slot']}")
    print(f"Standby slot:  {st['standby_slot']}")
    print(f"PID file:      {c(st['pid_file_exists'], 'exists')} ({PID_FILE})")
    print(f"Session age:  {st['session_age_sec']}s" if st['session_age_sec'] is not None else "Session age:  —")
    print(f"Pending switch: {c(st['has_pending_switch'], 'YES ⚠️', 'no')}")

    # 检查 slot 内容
    sm = SlotManager()
    for slot in (sm.current, sm.standby):
        has_config = slot.config.exists()
        has_memory = slot.memory.exists()
        has_sessions = slot.sessions.exists()
        print(f"\nSlot {slot.name}:")
        print(f"  config:   {c(has_config, '✓')}")
        print(f"  memory:   {c(has_memory, '✓')}")
        print(f"  sessions: {c(has_sessions, '✓')}")


@app.command()
def start(
    main_port: int = typer.Option(8080, "--main-port", help="Main gateway port"),
    shadow_port: int = typer.Option(8081, "--shadow-port", help="Shadow gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """启动 ARK 三层容灾系统"""
    import asyncio
    import logging
    import socket

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s | %(name)s | %(levelname)s | %(message)s"
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s"
        )

    # ── 启动前检查 ──────────────────────────────────────────────────

    def _port_available(port: int) -> bool:
        try:
            with socket.create_server(("localhost", port), reuse_addr=True):
                return True
        except OSError:
            return False

    # 1. 检查 slot 是否已初始化
    sm = SlotManager()
    if not sm._current.path.exists():
        print("[red]错误: Slot 未初始化，请先运行 'nanobot ark init'[/red]")
        raise SystemExit(1)

    # 2. 检查 gateway 是否已在运行（避免冲突）
    pid_file = Path.home() / ".nanobot" / "gateway.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            import os as _os
            _os.kill(pid, 0)  # 不抛异常说明进程活着
            print(f"[red]错误: gateway 已在运行 (PID {pid})，请先停止后再启动[/red]")
            print(f"  systemctl stop nanobot-gateway  # 如果用 systemd 管理")
            raise SystemExit(1)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pid_file.unlink()  # 清理失效 PID 文件

    # 3. 检查端口是否可用
    if not _port_available(main_port):
        print(f"[red]错误: 端口 {main_port} 已被占用，请先释放后再启动[/red]")
        raise SystemExit(1)
    if not _port_available(shadow_port):
        print(f"[red]错误: 端口 {shadow_port} 已被占用，请先释放后再启动[/red]")
        raise SystemExit(1)

    print(f"[green]Pre-flight checks passed, starting ARK...[/green]")

    async def _run():
        orchestrator = ArkOrchestrator(main_port=main_port, shadow_port=shadow_port)
        try:
            await orchestrator.start()
            # 保持运行
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            await orchestrator.stop()

    asyncio.run(_run())


@app.command()
def init():
    """从当前 ~/.nanobot 初始化 Slot A（首次设置时使用）"""
    _log_setup()
    sm = SlotManager()
    sm.init_slots_from_current()
    print("[green]Slot A initialized from current ~/.nanobot[/green]")


@app.command()
def sync():
    """手动触发自检 + 同步当前槽位到备用槽位"""
    _log_setup()

    async def _run():
        sm = SlotManager()
        check = await sm.self_check()
        print(f"Self-check: {check.reason}")
        if check.passed:
            ok = await sm.sync_current_to_standby()
            if ok:
                print(f"[green]Synced {sm.standby.name} <- {sm.current.name} (now standby)[/green]")
        else:
            print("[red]Self-check failed, skipping sync[/red]")

    asyncio.run(_run())


@app.command()
def switch():
    """立即切换到备用槽位（触发 watchdog 执行）"""
    _log_setup()
    sm = SlotManager()
    ok = asyncio.run(sm.switch_to_standby())
    if ok:
        from nanobot.ark.slot_manager import PENDING_SWITCH_FILE
        data = json.loads(PENDING_SWITCH_FILE.read_text())
        print(f"[yellow]Pending switch to {data['target_slot']}, watchdog will execute[/yellow]")


@app.command()
def snapshots():
    """查看快照列表"""
    _log_setup()
    try:
        from .snapshot_manager import SnapshotManager
    except ImportError:
        print("[red]SnapshotManager not implemented yet (L2)[/red]")
        return
    sm = SnapshotManager()
    snaps = sm.list_snapshots()

    if not snaps:
        print("No snapshots found.")
        return

    print(f"{'ID':<25} {'Created':<20} {'Size':<8} {'Type':<6} {'Reason'}")
    print("-" * 80)
    for s in snaps:
        age = ""
        print(f"{s.id:<25} {s.created_at.strftime('%Y-%m-%d %H:%M'):<20} {s.size_mb:>6.1f}MB  {s.is_full and 'full' or 'incr' :<6}  {s.reason}")


@app.command()
def snapshot(reason: str = "manual"):
    """手动创建快照"""
    _log_setup()
    try:
        from .snapshot_manager import SnapshotManager
    except ImportError:
        print("[red]SnapshotManager not implemented yet (L2)[/red]")
        return

    async def _run():
        sm = SnapshotManager()
        s = await sm.create_snapshot(reason=reason)
        print(f"[green]Snapshot created: {s.id} ({s.size_mb:.1f}MB)[/green]")

    asyncio.run(_run())


@app.command()
def restore(snapshot_id: str):
    """从快照恢复"""
    _log_setup()
    try:
        from .snapshot_manager import SnapshotManager
    except ImportError:
        print("[red]SnapshotManager not implemented yet (L2)[/red]")
        return

    async def _run():
        sm = SnapshotManager()
        ok = await sm.restore_snapshot(snapshot_id)
        if ok:
            print(f"[green]✓[/green] Restored from snapshot: {snapshot_id}")
            print(f"[yellow]⚠ 需要重启 gateway 生效: systemctl restart nanobot-gateway[/yellow]")
        else:
            print(f"[red]Snapshot not found or restore failed: {snapshot_id}[/red]")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
