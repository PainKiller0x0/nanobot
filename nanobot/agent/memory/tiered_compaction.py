"""Aggressive compaction strategy (REPAIRED FULL)."""
from __future__ import annotations
import re

# Lowered thresholds for near-instant responses
_LAYER2_THRESHOLD = 4_000  # Truncate anything above ~1k tokens
_LAYER3_MIN = 1_000
_LAYER3_MAX_LINES = 20 

TRUNCATED_MARKER = "<!-- ~NANOBOT_TRUNCATED~ {size} chars -->"

def layer2_truncate(result: str, max_chars: int = _LAYER2_THRESHOLD) -> str:
    if len(result) <= max_chars: return result
    excess = len(result) - max_chars
    return result[:max_chars] + "\n" + TRUNCATED_MARKER.format(size=excess)

def micro_compact(result: str, tool_name: str = "") -> str:
    if len(result) < _LAYER3_MIN and result.count("\n") <= _LAYER3_MAX_LINES:
        return result
    lines = result.splitlines()
    parts = [f"# {tool_name} output ({len(lines)} lines)"]
    if lines: parts.append(f"first: {lines[0][:200]}")
    if len(lines) > 5: parts.append("last: " + "\n".join(lines[-5:]))
    parts.append(f"(output aggressively compacted to save tokens)")
    return "\n".join(parts)

def needs_layer1(session_tokens, context_window, threshold):
    return session_tokens >= int(context_window * threshold)
