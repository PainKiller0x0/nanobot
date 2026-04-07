"""
L2: SnapshotManager — rsync 增量快照 (REPAIRED & ENHANCED)
- Adds Restore functionality for A/B Slots.
- Improved error handling.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

NANOBOT_ROOT = Path.home() / ".nanobot"
SNAPSHOT_BASE = NANOBOT_ROOT / "ark" / "snapshots"
INDEX_FILE = NANOBOT_ROOT / "ark" / "snapshots.json"
MAX_SNAPSHOTS = 10  # Increased for safety
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
    SOURCE_PATH = NANOBOT_ROOT
    SNAPSHOT_BASE = SNAPSHOT_BASE
    INDEX_FILE = INDEX_FILE

    def __init__(self):
        self._snapshots: List[Snapshot] = []
        self.SNAPSHOT_BASE.mkdir(parents=True, exist_ok=True)
        self._load_index()

    def _load_index(self):
        if self.INDEX_FILE.exists():
            try:
                data = json.loads(self.INDEX_FILE.read_text())
                self._snapshots = [
                    Snapshot(id=s["id"], path=Path(s["path"]), created_at=datetime.fromisoformat(s["created_at"]),
                             size_mb=s.get("size_mb", 0.0), is_full=s.get("is_full", False), reason=s.get("reason", "manual"))
                    for s in data.get("snapshots", [])
                ]
                # Filter out deleted snapshots
                self._snapshots = [s for s in self._snapshots if s.path.exists()]
            except Exception as e:
                logger.warning(f"Failed to load snapshot index: {e}")

    def _save_index(self):
        data = {"snapshots": [{"id": s.id, "path": str(s.path), "created_at": s.created_at.isoformat(),
                               "size_mb": s.size_mb, "is_full": s.is_full, "reason": s.reason} for s in self._snapshots]}
        self.INDEX_FILE.write_text(json.dumps(data, indent=2))

    async def create_snapshot(self, reason: str = "manual") -> Snapshot:
        """Create a new snapshot using rsync with hard links to previous one."""
        snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{reason}"
        target = self.SNAPSHOT_BASE / snapshot_id
        target.mkdir(parents=True, exist_ok=True)

        prev = self._snapshots[-1] if self._snapshots else None
        
        # Determine if we need a full copy (no previous snapshot or too old)
        is_full = True
        if prev and prev.path.exists():
            days_since = (datetime.now() - prev.created_at).days
            if days_since < FULL_INTERVAL_DAYS:
                is_full = False

        success = False
        if not is_full and RSYNC_AVAILABLE:
            success = await self._rsync_incremental(prev.path, target)
        
        if not success:
            logger.info(f"Creating full snapshot: {snapshot_id}")
            self._full_copy(target)
            is_full = True

        snapshot = Snapshot(id=snapshot_id, path=target, created_at=datetime.now(), size_mb=0.0, is_full=is_full, reason=reason)
        self._snapshots.append(snapshot)
        self._cleanup_old()
        self._save_index()
        logger.info(f"Snapshot created: {snapshot_id} (full={is_full})")
        return snapshot

    async def restore_to_slot(self, snapshot_id: str, slot_path: Path) -> bool:
        """Restore a snapshot back to a specific Slot directory."""
        snapshot = next((s for s in self._snapshots if s.id == snapshot_id), None)
        if not snapshot:
            logger.error(f"Restore failed: Snapshot {snapshot_id} not found")
            return False

        logger.info(f"Restoring snapshot {snapshot_id} to {slot_path}...")
        try:
            slot_path.mkdir(parents=True, exist_ok=True)
            # Use rsync to mirror the snapshot back, EXCLUDING THE SNAPSHOTS THEMSELVES
            result = subprocess.run([
                "rsync", "-a", "--delete",
                "--exclude=ark/snapshots",
                f"{snapshot.path}/",
                f"{slot_path}/"
            ], capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0:
                logger.info(f"Restore completed successfully to {slot_path}")
                return True
            else:
                logger.error(f"Rsync restore failed: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"Restore error: {e}")
            return False

    async def _rsync_incremental(self, prev_path: Path, target: Path) -> bool:
        try:
            # Use --link-dest to create hard links for unchanged files
            result = subprocess.run([
                "rsync", "-a", "--delete",
                f"--link-dest={prev_path}",
                "--exclude=ark/snapshots",
                "--exclude=ark/snapshots.json",
                "--exclude=*.log",  # Don't waste space on logs
                "--exclude=*.pid",
                f"{self.SOURCE_PATH}/",
                f"{target}/"
            ], capture_output=True, text=True, timeout=300)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Incremental rsync failed: {e}")
            return False

    def _full_copy(self, target: Path):
        def _ignore(path, names):
            rel_path = Path(path).relative_to(self.SOURCE_PATH)
            if "ark/snapshots" in str(rel_path):
                return names
            ignored = [n for n in names if n.endswith(".log") or n.endswith(".pid")]
            if "snapshots" in names: ignored.append("snapshots")
            return ignored
            
        shutil.copytree(self.SOURCE_PATH, target, dirs_exist_ok=True, ignore=_ignore)

    def _cleanup_old(self):
        while len(self._snapshots) > MAX_SNAPSHOTS:
            old = self._snapshots.pop(0)
            try:
                if old.path.exists(): shutil.rmtree(old.path)
                logger.info(f"Cleaned up old snapshot: {old.id}")
            except Exception as e:
                logger.warning(f"Failed to delete old snapshot {old.id}: {e}")

    def list_snapshots(self) -> List[Snapshot]:
        return sorted(self._snapshots, key=lambda s: s.created_at, reverse=True)

    def get_latest(self) -> Optional[Snapshot]:
        snaps = self.list_snapshots()
        return snaps[0] if snaps else None
