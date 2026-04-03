"""
RTK-style output filter registry.

Filters are applied to command outputs before they are stored in the
agent's context, dramatically reducing token usage while preserving
the information the LLM actually needs.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from loguru import logger

from nanobot.filters.token_tracker import TokenTracker


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Global tracker instance (lazily created)
_tracker: TokenTracker | None = None


def get_tracker(workspace: Path | str | None = None) -> TokenTracker:
    global _tracker
    if _tracker is None:
        _tracker = TokenTracker(workspace)
    return _tracker


def filter_output(
    cmd: str,
    raw_output: str,
    exit_code: int = 0,
    exec_ms: int = 0,
    workspace: Path | str | None = None,
) -> str:
    """
    Apply RTK filters to a command's raw output.

    Resolution order:
      1. Exact alias match (e.g. 'git status' → git_status_filter)
      2. Prefix alias match (e.g. 'git log' → git_log_filter)
      3. Command-name match (e.g. 'pytest' → pytest_filter)
      4. Fallback: return raw output

    Results are tracked via TokenTracker.
    """
    from nanobot.filters.tee import TeeFilter

    t0 = time.perf_counter()
    filtered = _apply_filters(cmd, raw_output)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Apply Tee: save raw output when command failed and was significantly compressed
    tee = TeeFilter(workspace)
    filtered = tee.restore_if_needed(cmd, raw_output, filtered, exit_code)

    # Track savings
    try:
        tracker = get_tracker(workspace)
        tracker.track(cmd, raw_output, filtered, exit_code, exec_ms or elapsed_ms)
        if logger.isEnabledFor(15):  # DEBUG level
            pct = _savings_pct(raw_output, filtered)
            logger.debug(
                "RTK filter: {} chars → {} chars ({}% saved, {}ms)",
                len(raw_output), len(filtered), pct, elapsed_ms,
            )
    except Exception:
        pass  # Never fail the main path due to tracking

    return filtered


# ---------------------------------------------------------------------------
# Filter registry
# ---------------------------------------------------------------------------

# Signature: (raw_output: str, exit_code: int) → str
FilterFn = Callable[[str, int], str]


def _apply_filters(cmd: str, raw_output: str) -> str:
    """Dispatch to the best-matching filter."""

    # 1. Exact alias
    if alias := _exact_alias(cmd):
        return alias(raw_output, 0)

    # 2. Prefix alias (try from longest key to shortest)
    for key, fn in sorted(_PREFIX_ALIASES, key=lambda x: -len(x[0])):
        if cmd.startswith(key):
            return fn(raw_output, 0)

    # 3. Command-name match
    first_word = cmd.strip().split()[0]
    if fn := _COMMAND_FILTERS.get(first_word):
        return fn(raw_output, 0)

    # 4. No filter
    return raw_output


def _savings_pct(raw: str, filtered: str) -> str:
    if not raw:
        return "0"
    saved = len(raw) - len(filtered)
    return f"{saved / len(raw) * 100:.0f}"


# ---------------------------------------------------------------------------
# Aliases: maps command patterns to their filter functions
# ---------------------------------------------------------------------------

def _exact_alias(cmd: str) -> FilterFn | None:
    """Return filter for exact command match."""
    # Normalize
    normalized = cmd.strip()
    return _EXACT_ALIASES.get(normalized)


_EXACT_ALIASES: dict[str, FilterFn] = {}
_PREFIX_ALIASES: list[tuple[str, FilterFn]] = []
_COMMAND_FILTERS: dict[str, FilterFn] = {}


def _alias(cmd_pattern: str, fn: FilterFn) -> None:
    """Decorator to register a command alias."""
    if " " in cmd_pattern:
        _EXACT_ALIASES[cmd_pattern] = fn
    else:
        _COMMAND_FILTERS[cmd_pattern] = fn


# ---------------------------------------------------------------------------
# Individual filters
# ---------------------------------------------------------------------------

def _git_filter(output: str, _exit: int) -> str:
    """Route to the appropriate git sub-filter."""
    return GitFilter.filter(output)


class GitFilter:
    """RTK Stats Extraction for git commands — 75-92% token savings."""

    @staticmethod
    def filter(output: str) -> str:
        lines = output.strip().splitlines()
        if not lines:
            return "(empty)"

        # Detect sub-command from first line patterns
        first = lines[0].strip()
        if first.startswith("On branch") or first in ("", "nothing to commit"):
            return GitStatus.filter(output)
        if "diff --git" in output:
            return GitDiff.filter(output)
        if re.match(r"[a-f0-9]+ \(", first) or re.match(r"[a-f0-9]{7,40}", first):
            return GitLog.filter(output)
        if "fatal:" in first or "error:" in first.lower():
            return f"[git error] {output.strip()[:500]}"

        # Generic: return first line + line count
        return f"{len(lines)} lines: {lines[0][:200]}"

    @classmethod
    def status(cls, output: str) -> str:
        return cls.filter(output)

    @classmethod
    def diff(cls, output: str) -> str:
        return cls.filter(output)

    @classmethod
    def log(cls, output: str) -> str:
        return cls.filter(output)


class GitStatus:
    """Parse git status output."""

    @staticmethod
    def filter(output: str) -> str:
        lines = output.strip().splitlines()
        if not lines:
            return "(clean)"

        # Clean tree
        if any("nothing to commit" in l for l in lines[:2]):
            return "(clean)"

        staged = modified = untracked = deleted = conflicts = 0
        files: dict[str, str] = {}

        for line in lines:
            if not line:
                continue
            if line.startswith("On branch"):
                continue
            if line.startswith("Changes not staged"):
                continue
            if line.startswith("Untracked files"):
                continue
            if line.startswith("Changes to be committed"):
                continue
            if line.startswith("Conflicts"):
                continue
            if re.match(r"^\s*$", line):
                continue

            # Staged
            m = re.match(r"^\s*([MADRC])\s+(.+)$", line)
            if m:
                staged += 1
                files[m.group(2)] = m.group(1)
                continue
            # Unstaged modified
            m = re.match(r"^\s*( M|MM)\s+(.+)$", line)
            if m:
                modified += 1
                continue
            # Untracked
            m = re.match(r"^\?\?\s+(.+)$", line)
            if m:
                untracked += 1
                continue
            # Deleted
            m = re.match(r"^\s*D\s+(.+)$", line)
            if m:
                deleted += 1
                continue
            # Both modified
            m = re.match(r"^\s*(AA|DU|MM)\s+(.+)$", line)
            if m:
                conflicts += 1
                continue

        parts: list[str] = []
        if staged:
            parts.append(f"+{staged}")
        if modified:
            parts.append(f"~{modified}")
        if deleted:
            parts.append(f"-{deleted}")
        if untracked:
            parts.append(f"?{untracked}")
        if conflicts:
            parts.append(f"!{conflicts}")

        if not parts:
            return "(clean)"

        tag = "[" + " ".join(parts) + "]"
        # Show a sample file for context
        sample = next(iter(files), None)
        if sample:
            return f"{tag} {sample}"
        return tag


class GitDiff:
    """Parse git diff output."""

    @staticmethod
    def filter(output: str) -> str:
        if not output.strip():
            return "(no diff)"

        diffs = re.split(r"(?=diff --git)", output.strip())
        results: list[str] = []
        total_added = total_removed = 0

        for diff in diffs:
            lines = diff.splitlines()
            if not lines:
                continue

            # File header
            m = re.search(r"diff --git a/(.+?) b/(.+)", lines[0] if lines else "")
            filename = m.group(2) if m else "?"
            added = removed = 0
            hunk_info = ""

            for line in lines:
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
                elif line.startswith("@@"):
                    hunk_info += " " + line

            total_added += added
            total_removed += removed

            if added == 0 and removed == 0:
                results.append(f"  {filename} (binary or mode)")
            else:
                delta = f"+{added}/-{removed}" if removed else f"+{added}"
                snippet = hunk_info[:80] if hunk_info else ""
                results.append(f"  {filename} {delta}{snippet}")

        if not results:
            return "(no changes)"

        header = f"[+{total_added}/-{total_removed} across {len(results)} files]"
        if len(results) > 10:
            return header + "\n" + "\n".join(results[:10]) + f"\n  ... {len(results)-10} more files"
        return header + "\n" + "\n".join(results)


class GitLog:
    """Parse git log output."""

    @staticmethod
    def filter(output: str) -> str:
        lines = [l for l in output.strip().splitlines() if l]
        if not lines:
            return "(no commits)"

        count = len(lines)

        # Oneline format: "hash message"
        if re.match(r"[a-f0-9]{7,40}\s", lines[0]):
            hashes = []
            messages = []
            for line in lines:
                parts = line.split(" ", 1)
                hashes.append(parts[0][:7])
                if len(parts) > 1:
                    messages.append(parts[1])
            summary = ", ".join(hashes[:5])
            if count > 5:
                summary += f" (+{count - 5} more)"
            if messages:
                summary += f"\n  latest: {messages[0][:80]}"
            return summary

        # Full format: try to extract commit stats
        commits = output.strip().split("\n\n")
        if len(commits) > 1:
            return f"{count} commits"

        return f"{count} commits: {lines[0][:100]}"


# ---------------------------------------------------------------------------
# Pytest filter
# ---------------------------------------------------------------------------

def _pytest_filter(output: str, exit_code: int) -> str:
    return PytestFilter.filter(output, exit_code)


class PytestFilter:
    """
    RTK Failure Focus + State Machine strategy for pytest.
    90%+ token savings on test output.
    """

    # Summary line patterns
    _SUMMARY_PATTERNS = [
        r"(\d+)\s+passed",
        r"(\d+)\s+failed",
        r"(\d+)\s+error",
        r"(\d+) passed, (\d+) failed",
        r"(\d+) failed, (\d+) passed",
        r"(\d+) error",
    ]

    @classmethod
    def filter(cls, output: str, exit_code: int = 0) -> str:
        if not output.strip():
            return "(no output)"

        # 1. Check for fatal errors first
        fatal = cls._extract_fatal(output)
        if fatal:
            return f"[pytest FATAL] {fatal}"

        # 2. Parse summary
        summary = cls._parse_summary(output)

        # 3. Parse failures if any
        failures = cls._extract_failures(output)

        # 4. Build compact output
        lines: list[str] = []

        if summary:
            lines.append(summary)
        else:
            # Fallback: show last non-empty line
            non_empty = [l for l in output.splitlines() if l.strip()]
            if non_empty:
                lines.append(non_empty[-1][:120])

        if failures:
            lines.append("")
            lines.extend(failures)

        result = "\n".join(lines)
        if not result.strip():
            return output[:200]

        # If output is already small, don't filter
        if len(result) >= len(output) * 0.8:
            return output[:800]

        return result

    @staticmethod
    def _extract_fatal(output: str) -> str | None:
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith(("ERROR:   ", "INTERNALERROR")):
                return stripped[:300]
        return None

    @classmethod
    def _parse_summary(cls, output: str) -> str | None:
        last_lines = output.splitlines()[-5:]
        for line in last_lines:
            for pattern in cls._SUMMARY_PATTERNS:
                m = re.search(pattern, line)
                if m:
                    groups = m.groups()
                    if len(groups) == 1:
                        return f"pytest: {groups[0]} passed ✓"
                    elif len(groups) == 2:
                        if groups[1] and int(groups[1] or 0) > 0:
                            return f"pytest: {groups[1]} failed ✗, {groups[0]} passed"
                        return f"pytest: {groups[0]} passed ✓"
        return None

    @staticmethod
    def _extract_failures(output: str) -> list[str]:
        failures: list[str] = []
        lines = output.splitlines()
        in_failure = False
        current: list[str] = []

        for line in lines:
            if re.match(r"^(FAIL|PASS|ERROR|test_.*) ", line):
                in_failure = "FAIL" in line or "ERROR" in line
                current = [line.strip()]
            elif in_failure:
                if line.strip() and not line.startswith(" " * 16) and not line.startswith("="):
                    # End of this failure block
                    if current:
                        failures.append("  " + " | ".join(current)[:150])
                        current = []
                    in_failure = False
                elif "assert" in line.lower() or "Error" in line or "Failed" in line:
                    current.append(line.strip()[:80])

        if current:
            failures.append("  " + " | ".join(current)[:150])

        # Deduplicate
        seen: set[str] = set()
        unique: list[str] = []
        for f in failures:
            if f not in seen and len(unique) < 5:
                seen.add(f)
                unique.append(f)

        return unique


# ---------------------------------------------------------------------------
# Register filters
# ---------------------------------------------------------------------------

_alias("pytest", _pytest_filter)
_alias("pytest", _pytest_filter)  # Already registered above

# Register git sub-commands as prefix aliases (order: longest first)
_prefix_aliases = [
    ("git status", GitFilter.status),
    ("git diff", GitFilter.diff),
    ("git log", GitFilter.log),
    ("git branch", GitFilter.status),
    ("git stash list", GitFilter.status),
]
for key, fn in _prefix_aliases:
    _PREFIX_ALIASES.append((key, fn))

def _lint_filter(name: str) -> FilterFn:
    """Create a lint filter for a given linter."""
    def fn(output: str, _exit: int) -> str:
        lines = output.strip().splitlines()
        if not lines:
            return f"({name}: no output)"
        if "error" not in output.lower() and "warning" not in output.lower():
            return f"({name}: clean)"
        # Count by file
        by_file: dict[str, int] = {}
        for line in lines:
            m = re.match(rf"([^:\s]+:\d+)", line)
            if m:
                f = m.group(1)
                by_file[f] = by_file.get(f, 0) + 1
        if by_file:
            total = sum(by_file.values())
            files = ", ".join(sorted(by_file)[:5])
            suffix = ", ..." if len(by_file) > 5 else ""
            extra = f" ({total} issues in {files}{suffix})"
            return f"{name}: {total} issues{extra}"
        return output[:300]
    return fn


# Register lint filters
_COMMAND_FILTERS["ruff"] = _lint_filter("ruff")
_COMMAND_FILTERS["eslint"] = _lint_filter("eslint")
_COMMAND_FILTERS["flake8"] = _lint_filter("flake8")


# Alias decorator shortcut
def alias(patterns: list[str]):
    """Decorator to register a command alias for one or more patterns."""
    def deco(fn: FilterFn):
        for p in patterns:
            if " " in p:
                _EXACT_ALIASES[p] = fn
            else:
                _COMMAND_FILTERS[p] = fn
        return fn
    return deco
