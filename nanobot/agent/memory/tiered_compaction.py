"""
Three-layer context compression system.

Inspired by Claude Code's tiered compaction strategy:

  Layer 1 — Session Memory:
    Triggered when session history exceeds compaction_threshold (default 75%).
    Archives older messages to persistent memory via MemoryConsolidator.
    This is the existing nanobot consolidation behaviour.

  Layer 2 — API Response Truncation:
    Triggered when a single tool result exceeds _LARGE_RESULT_CHARS (default 16 KB).
    Truncates with a "[truncated N chars]" marker.  Prevents one verbose result
    from consuming the entire context window.  Already partially implemented via
    _TOOL_RESULT_MAX_CHARS in AgentLoop; this module makes it explicit.

  Layer 3 — Micro-compact:
    Triggered after every tool call for results between 2 KB and 16 KB.
    Instead of storing the full verbose output, stores a structured key-value
    extraction (first line as summary, line count, last line as final state).
    Keeps context signal without the verbosity.

Usage:
    from nanobot.agent.memory.tiered_compaction import micro_compact

    compact_result = micro_compact(result_str, tool_name)
    # Returns the compact version or original if not worth compacting.
"""

from __future__ import annotations

import re

# Thresholds (in characters)
_LAYER2_THRESHOLD = 16_000  # Truncate above this
_LAYER3_MIN = 2_000  # Micro-compact above this, below Layer 2
_LAYER3_MAX_LINES = 50  # If result has >50 lines, definitely compact

# Boundary marker inserted by Layer 2 truncation
TRUNCATED_MARKER = "<!-- ~NANOBOT_TRUNCATED~ {size} chars -->"
SUMMARY_PREFIX = "[Earlier conversation summarized]\n"


def layer2_truncate(result: str, max_chars: int = _LAYER2_THRESHOLD) -> str:
    """
    Layer 2: truncate tool result if it exceeds max_chars.

    Replaces the tail of the result with a boundary marker so the agent
    knows truncation happened and doesn't assume the output was complete.
    """
    if len(result) <= max_chars:
        return result
    excess = len(result) - max_chars
    marker = TRUNCATED_MARKER.format(size=excess)
    return result[:max_chars] + "\n" + marker


def micro_compact(result: str, tool_name: str = "") -> str:
    """
    Layer 3: extract the essential signal from a verbose tool result.

    Strategy:
      - First line  → intent / command that was run
      - Line count  → scope of output
      - Last 5 lines → final state (exit codes, file states, etc.)
      - Diff/file-match hits → list up to 20 items
      - Everything else → replaced with "(N lines of output)"

    Only applied when result is between LAYER3_MIN and LAYER2_THRESHOLD chars,
    or has more than LAYER3_MAX_LINES lines.
    """
    if len(result) < _LAYER3_MIN and result.count("\n") <= _LAYER3_MAX_LINES:
        return result

    lines = result.splitlines()
    n_lines = len(lines)

    parts: list[str] = []
    parts.append(f"# {tool_name} output ({n_lines} lines)")

    # First line: command / intent
    if lines:
        parts.append(f"first: {lines[0][:200]}")

    # Last 5 lines: final state
    if n_lines > 5:
        tail = lines[-5:]
        parts.append("last: " + "\n".join(tail))

    # git diff / grep hits: extract key filenames
    diff_hits = _extract_diff_hits(lines)
    if diff_hits:
        parts.append(f"changed: {', '.join(diff_hits[:20])}")

    # Error/warning summary
    errors = [l for l in lines if l.strip().startswith(("error", "ERROR", "Error", "failed", "FAILED", "Failed", "fatal", "FATAL"))]
    if errors:
        parts.append(f"errors: {'; '.join(errors[:5])}")

    # Byte/line count for large dumps
    if n_lines > _LAYER3_MAX_LINES:
        parts.append(f"(output truncated to key signals; original had {n_lines} lines)")

    return "\n".join(parts)


def _extract_diff_hits(lines: list[str]) -> list[str]:
    """Extract file paths from git diff / grep -l output."""
    hits: list[str] = []

    def _add(path: str) -> None:
        # Strip common prefixes
        path = re.sub(r"^(diff --git|---|\+\+\+)\s+[a/]/", "", path)
        path = path.strip()
        if path and path not in ("/dev/null", "a", "b") and not path.startswith("-"):
            hits.append(path)

    for line in lines:
        line = line.rstrip()
        if line.startswith(("+++ ", "-- ")):
            _add(line[4:])
        elif line.startswith("diff --git"):
            # Next few lines have --- and +++ with filenames
            pass
        elif line.startswith("Binary files"):
            # Binary file changes
            m = re.search(r"Binary files ([^\s]+)", line)
            if m:
                hits.append(m.group(1))

    return hits


def needs_layer1(session_tokens: int, context_window: int, threshold: float) -> bool:
    """Return True when session history should trigger Layer 1 consolidation."""
    return session_tokens >= int(context_window * threshold)
