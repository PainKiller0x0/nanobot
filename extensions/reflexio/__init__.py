"""Optional Reflexio extension module for nanobot."""

from __future__ import annotations

import os

import httpx
from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext


class ReflexioHook(AgentHook):
    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus
        self.url = os.environ.get("REFLEXIO_URL", "http://reflexio-sidecar:8081")

    async def after_iteration(self, context: AgentHookContext) -> None:
        if not (context.stop_reason and context.final_content):
            return
        try:
            user_msg = None
            for msg in reversed(context.messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    user_msg = " ".join(text_parts).strip()
                else:
                    user_msg = str(content)
                break

            if not user_msg:
                return

            if "[Runtime Context" in user_msg:
                marker = "[/Runtime Context]"
                idx = user_msg.find(marker)
                if idx != -1:
                    user_msg = user_msg[idx + len(marker):].strip()

            payload = {
                "user_id": "default_user",
                "interaction_data_list": [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": context.final_content},
                ],
                "session_id": "default_session",
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.url}/api/publish_interaction",
                    json=payload,
                    timeout=5.0,
                )
                if resp.is_success:
                    logger.info("Reflexio extension: interaction published")
                else:
                    logger.warning("Reflexio extension publish failed: {}", resp.status_code)
        except Exception as e:
            logger.error("Reflexio extension hook error: {}", e)


def build_context_block(current_message: str) -> str | None:
    if not current_message or len(current_message.strip()) < 2:
        return None

    url = os.environ.get("REFLEXIO_URL", "http://reflexio-sidecar:8081")
    try:
        resp = httpx.post(
            f"{url}/api/search",
            json={"query": current_message, "limit": 5},
            timeout=3.0,
        )
        if not resp.is_success:
            return None
        data = resp.json()
        results = data.get("results", [])
        relevant = [r for r in results if r.get("score", 0) >= 0.5]
        if not relevant:
            return None
        lines = [
            "[Memory - Retrieved by Reflexio extension from past conversations. Use naturally.]",
        ]
        for r in relevant:
            kind = r.get("kind", "memory")
            content_text = r.get("content", "")
            score = r.get("score", 0)
            lines.append(f"- ({kind}, relevance: {score:.0%}) {content_text}")
        lines.append("[/Memory]")
        return "\n".join(lines)
    except Exception:
        return None


def build_agent_hooks(bus=None):
    return [ReflexioHook(bus=bus)]
