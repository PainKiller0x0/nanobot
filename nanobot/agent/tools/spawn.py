"""Spawn tool for creating background subagents."""

import base64
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import DangerLevel, Permission, Tool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


class SpawnTool(Tool):
    danger_level = DangerLevel.HIGH
    permission = Permission.DENY
    """Tool to spawn a subagent for background task execution."""

    # Max attachment size: 10MB
    MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful. "
            "Can attach files to provide context to the subagent."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (for display)",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to attach (absolute or relative to workspace). "
                                   "Text files will be inlined, binary files base64-encoded. "
                                   "Max 10MB per file.",
                },
            },
            "required": ["task"],
        }

    def _read_attachment(self, path: str) -> dict[str, Any]:
        """Read a file and return metadata with content."""
        from nanobot.utils.helpers import ensure_dir

        # Resolve path relative to workspace if not absolute
        if not os.path.isabs(path):
            path = os.path.join(str(self._manager.workspace), path)

        full_path = Path(path).expanduser().resolve()

        # Security: ensure path is within workspace
        try:
            full_path.relative_to(self._manager.workspace)
        except ValueError:
            raise ValueError(f"Attachment path '{path}' is outside workspace")

        if not full_path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")

        size = full_path.stat().st_size
        if size > self.MAX_ATTACHMENT_SIZE:
            raise ValueError(f"Attachment '{path}' exceeds max size of {self.MAX_ATTACHMENT_SIZE // (1024*1024)}MB")

        # Detect mime type
        suffix = full_path.suffix.lower()
        mime_types = {
            '.txt': 'text/plain', '.md': 'text/markdown', '.json': 'application/json',
            '.yaml': 'application/yaml', '.yml': 'application/yaml', '.py': 'text/x-python',
            '.js': 'application/javascript', '.ts': 'application/typescript',
            '.html': 'text/html', '.css': 'text/css', '.xml': 'application/xml',
            '.csv': 'text/csv', '.log': 'text/plain', '.sh': 'text/x-shellscript',
        }
        mime_type = mime_types.get(suffix, 'application/octet-stream')

        # Read content
        text_extensions = {'.txt', '.md', '.json', '.yaml', '.yml', '.py', '.js', '.ts',
                          '.html', '.css', '.xml', '.csv', '.log', '.sh', '.c', '.cpp',
                          '.h', '.hpp', '.go', '.rs', '.java', '.kt', '.swift'}
        is_text = suffix in text_extensions

        if is_text:
            content = full_path.read_text(encoding='utf-8', errors='replace')
        else:
            content = base64.b64encode(full_path.read_bytes()).decode('ascii')

        return {
            "name": full_path.name,
            "path": str(full_path),
            "mime_type": mime_type,
            "is_text": is_text,
            "content": content,
            "size": size,
        }

    async def execute(
        self,
        task: str,
        label: str | None = None,
        attachments: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task with optional attachments."""
        attachment_data = None
        if attachments:
            attachment_data = []
            for path in attachments:
                try:
                    data = self._read_attachment(path)
                    attachment_data.append(data)
                except (FileNotFoundError, ValueError) as e:
                    return f"Error: {e}"

        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            session_key=self._session_key,
            attachments=attachment_data,
        )
