"""Task management tool for background tasks."""

from typing import Any

from nanobot.agent.tools.base import Tool


class TaskTool(Tool):
    """Tool to manage background tasks (list status, cancel)."""

    def __init__(self, loop):
        self._loop = loop

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Manage background tasks. Actions: list (show all tasks and their status), "
            "cancel (cancel a specific task by ID)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "cancel"],
                    "description": "Action to perform",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID to cancel (required for cancel action)",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "list":
            return self._list_tasks()
        elif action == "cancel":
            return self._cancel_task(task_id)
        return f"Unknown action: {action}"

    def _list_tasks(self) -> str:
        tasks = self._loop._task_registry.list_tasks()
        if not tasks:
            return "No background tasks running."

        lines = ["Background tasks:"]
        for tid, info in tasks.items():
            status_icon = {
                "pending": "⏳",
                "running": "🔄",
                "completed": "✅",
                "cancelled": "🚫",
                "failed": "❌",
            }.get(info.status, "❓")
            error_msg = f" (error: {info.error})" if info.error else ""
            lines.append(f"  {status_icon} {info.name} (id: {tid}, status: {info.status}){error_msg}")
        return "\n".join(lines)

    def _cancel_task(self, task_id: str | None) -> str:
        if not task_id:
            return "Error: task_id is required for cancel"
        if self._loop.cancel_background_task(task_id):
            return f"Cancelled task {task_id}"
        return f"Task {task_id} not found or already completed"
