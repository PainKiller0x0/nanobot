"""
RTK Tee system — save raw output when commands fail.

If a command fails (exit code != 0) and the filtered output is
significantly smaller than the raw output, the raw output is saved
to a tee file so the LLM can read the full details if needed.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from loguru import logger

# Save raw output when: command failed AND compression ratio > 2x
_TEE_MIN_COMPRESSION = 2.0


class TeeFilter:
    """
    Save raw command output on failure for later inspection.

    The agent receives a compact filtered result, but if it needs
    the full details it can read the tee file directly.
    """

    def __init__(self, workspace: Path | str | None = None):
        if workspace is None:
            workspace = Path.home() / ".nanobot"
        elif isinstance(workspace, str):
            workspace = Path(workspace)
        self._tee_dir = workspace / "filters" / "tee"
        self._tee_dir.mkdir(parents=True, exist_ok=True)

    def restore_if_needed(
        self,
        cmd: str,
        raw_output: str,
        filtered_output: str,
        exit_code: int,
    ) -> str:
        """
        If the command failed and output was heavily compressed,
        save raw output to a tee file and append the path.

        Returns the (possibly modified) filtered output.
        """
        if exit_code == 0:
            return filtered_output

        if len(filtered_output) >= len(raw_output):
            return filtered_output

        ratio = len(raw_output) / max(1, len(filtered_output))
        if ratio < _TEE_MIN_COMPRESSION:
            return filtered_output

        safe_name = self._sanitize(cmd)
        ts = int(time.time())
        tee_file = self._tee_dir / f"{ts}_{safe_name}.log"

        try:
            tee_file.write_text(raw_output, encoding="utf-8")
            suffix = f"\n[full output saved to: {tee_file}]"
            # Avoid duplicating the note if already present
            if suffix not in filtered_output:
                return filtered_output + suffix
        except OSError:
            logger.exception("TeeFilter: failed to write {}", tee_file)

        return filtered_output

    @staticmethod
    def _sanitize(cmd: str) -> str:
        """Make a command safe to use as a filename."""
        # Keep alphanumeric, hyphens, underscores; truncate to 50 chars
        safe = re.sub(r"[^\w\-]", "_", cmd)[:50]
        return safe.strip("_") or "cmd"
