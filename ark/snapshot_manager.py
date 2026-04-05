"""
L2: SnapshotManager — rsync 增量快照

策略:
- 首次快照: 全量复制
- 后续快照: rsync --link-dest 硬链接增量（节省空间）
- 每 7 天强制全量
- 保留最近 5 个快照
- rsync 不可用时 fallback 到 shutil.copytree
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

NANOBOT_ROOT = Path.home() / ".nanobot"
SNAPSHOT_BASE = NANOBOT_ROOT / "ark" / "snapshots"
INDEX_FILE = NANOBOT_ROOT / "ark" / "snapshots.json"
MAX_SNAPSHOTS = 5
FULL_INTERVAL_DAYS = 7
RSYNC_AVAILABLE = shutil.which("rsync") is not None


@dataclass
class Snapshot:
    id: str
    path: Path
    created_at: datetime
    size_mb: float
    is_full: bool
    reason: str


class SnapshotManager:
    """
    L2: rsync + reflink 增量快照管理器
    """

    SOURCE_PATH = NANOBOT_ROOT
    SNAPSHOT_BASE = SNAPSHOT_BASE
    INDEX_FILE = INDEX_FILE
    MAX_SNAPSHOTS = MAX_SNAPSHOTS
    FULL_INTERVAL_DAYS = FULL_INTERVAL_DAYS

    def __init__(self):
        self._snapshots: List[Snapshot] = []
        self.SNAPSHOT_BASE.mkdir(parents=True, exist_ok=True)
        self._load_index()

    # ── 索引 ────────────────────────────────────────────────────────────

    def _load_index(self):
        """加载快照索引"""
        if self.INDEX_FILE.exists():
            try:
                data = json.loads(self.INDEX_FILE.read_text())
                self._snapshots = [
                    Snapshot(
                        id=s["id"],
                        path=Path(s["path"]),
                        created_at=datetime.fromisoformat(s["created_at"]),
                        size_mb=s["size_mb"],
                        is_full=s["is_full"],
                        reason=s["reason"]
                    )
                    for s in data.get("snapshots", [])
                ]
                # 只保留存在的快照
                self._snapshots = [s for s in self._snapshots if s.path.exists()]
                self._save_index()
            except Exception as e:
                logger.warning(f"Failed to load snapshot index: {e}")

    def _save_index(self):
        """保存快照索引"""
        data = {
            "snapshots": [
                {
                    "id": s.id,
                    "path": str(s.path),
                    "created_at": s.created_at.isoformat(),
                    "size_mb": s.size_mb,
                    "is_full": s.is_full,
                    "reason": s.reason
                }
                for s in self._snapshots
            ]
        }
        self.INDEX_FILE.write_text(json.dumps(data, indent=2))

    def _get_latest(self) -> Optional[Snapshot]:
        """获取最新快照"""
        return self._snapshots[-1] if self._snapshots else None

    def _calc_dir_size(self, path: Path) -> float:
        """计算目录大小 (MB)"""
        total = 0
        try:
            for p in path.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
        except Exception as e:
            logger.warning(f"Error calculating dir size: {e}")
        return total / (1024 * 1024)

    # ── 创建快照 ───────────────────────────────────────────────────────

    async def create_snapshot(self, reason: str = "manual") -> Snapshot:
        """创建增量快照"""
        snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{reason}"
        target = self.SNAPSHOT_BASE / snapshot_id
        target.mkdir(parents=True, exist_ok=True)

        prev = self._get_latest()
        is_full = False

        if prev and prev.path.exists():
            # 检查是否需要全量
            days_since = (datetime.now() - prev.created_at).days
            is_full = days_since >= self.FULL_INTERVAL_DAYS or not prev.is_full

            if not is_full and RSYNC_AVAILABLE:
                # 增量快照: rsync --link-dest
                ok = await self._rsync_incremental(prev.path, target)
                if not ok:
                    logger.warning("rsync incremental failed, falling back to full copy")
                    shutil.rmtree(target)
                    target.mkdir(parents=True, exist_ok=True)
                    is_full = True
            elif not is_full:
                # rsync 不可用，用 reflink copy
                is_full = await self._reflink_copy(target, prev.path)

        if is_full or not prev:
            self._full_copy(target)
            is_full = True

        size = self._calc_dir_size(target)
        snapshot = Snapshot(
            id=snapshot_id,
            path=target,
            created_at=datetime.now(),
            size_mb=size,
            is_full=is_full,
            reason=reason
        )
        self._snapshots.append(snapshot)
        self._save_index()

        # 清理旧快照
        self._cleanup_old()

        logger.info(
            f"Snapshot created: {snapshot_id} "
            f"({'full' if is_full else 'incremental'}, {size:.1f}MB)"
        )
        return snapshot

    async def _rsync_incremental(self, prev_path: Path, target: Path) -> bool:
        """rsync --link-dest 增量快照"""
        try:
            result = subprocess.run(
                [
                    "rsync", "-a", "--delete",
                    f"--link-dest={prev_path}",
                    f"{self.SOURCE_PATH}/",
                    f"{target}/"
                ],
                capture_output=True,
                text=True,
                timeout=300  # 5min timeout
            )
            if result.returncode == 0:
                return True
            logger.warning(f"rsync failed: {result.stderr}")
            return False
        except Exception as e:
            logger.warning(f"rsync error: {e}")
            return False

    async def _reflink_copy(self, target: Path, prev_path: Path) -> bool:
        """用 reflink copy 做增量（rsync 不可用时的 fallback）"""
        try:
            # cp --reflink=auto 复制整个目录
            result = subprocess.run(
                ["cp", "-a", "--reflink=auto", str(self.SOURCE_PATH), str(target)],
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"reflink copy failed: {e}")
            return False

    def _full_copy(self, target: Path):
        """全量复制"""
        shutil.copytree(self.SOURCE_PATH, target, dirs_exist_ok=True)

    # ── 恢复 ───────────────────────────────────────────────────────────

    async def restore_snapshot(self, snapshot_id: str) -> bool:
        """从快照恢复文件。恢复后需重启 gateway 生效。"""
        """从快照恢复"""
        snap = self._find_snapshot(snapshot_id)
        if not snap:
            logger.error(f"Snapshot not found: {snapshot_id}")
            return False

        if not snap.path.exists():
            logger.error(f"Snapshot path not found: {snap.path}")
            return False

        logger.info(f"Restoring from snapshot: {snapshot_id}")

        # 恢复前先备份当前状态（如果存在）
        backup_dir = self.SNAPSHOT_BASE / f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        current = self.SOURCE_PATH
        if current.exists() and any(current.iterdir()):
            logger.info(f"Backing up current state to {backup_dir}")
            shutil.copytree(current, backup_dir, dirs_exist_ok=True)

        # 恢复快照
        try:
            # 删除当前目录内容（除了 snapshots）
            for item in current.iterdir():
                if item.name == "ark" and item.is_dir():
                    continue  # 保留 ark 目录
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

            # 复制快照内容
            for item in snap.path.iterdir():
                if item.name == "ark":
                    continue  # 跳过 ark
                dst = current / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)

            logger.info(f"Snapshot restored: {snapshot_id}")
            # 清理 gateway.pid，恢复后 gateway 进程 PID 肯定变了
            old_pid = NANOBOT_ROOT / "gateway.pid"
            if old_pid.exists():
                old_pid.unlink()
            return True

        except Exception as e:
            logger.error(f"Snapshot restore failed: {e}")
            return False

    # ── 列表 ───────────────────────────────────────────────────────────

    def list_snapshots(self) -> List[Snapshot]:
        """列出所有快照"""
        return list(self._snapshots)

    def _find_snapshot(self, snapshot_id: str) -> Optional[Snapshot]:
        """按 ID 查找快照"""
        for s in self._snapshots:
            if s.id == snapshot_id:
                return s
        return None

    # ── 清理 ───────────────────────────────────────────────────────────

    def _cleanup_old(self):
        """清理超过 MAX_SNAPSHOTS 的旧快照"""
        while len(self._snapshots) > self.MAX_SNAPSHOTS:
            old = self._snapshots.pop(0)
            try:
                if old.path.exists():
                    shutil.rmtree(old.path)
                logger.info(f"Cleaned up old snapshot: {old.id}")
            except Exception as e:
                logger.warning(f"Failed to clean up snapshot {old.id}: {e}")
        self._save_index()
