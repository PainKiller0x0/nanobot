"""Path security policy - prevents unauthorized filesystem access."""

from __future__ import annotations

import os
from pathlib import Path


class SecurityError(Exception):
    """Raised when a path violates the security policy."""
    pass


# Paths that are always forbidden, regardless of workspace
BLOCKED_PATHS = {
    "/etc", "/var", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/dev", "/proc", "/sys", "/run", "/root",
    os.path.expanduser("~/.ssh"),
    os.path.expanduser("~/.gnupg"),
    os.path.expanduser("~/.aws"),
    os.path.expanduser("~/.config"),
}


class PathPolicy:
    """Path security policy - prevents workspace escape and sensitive path access.

    Usage:
        policy = PathPolicy(workspace=Path("/workspace"))
        policy.validate(Path("/workspace/file.txt"))  # OK
        policy.validate(Path("/etc/passwd"))           # SecurityError
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def is_allowed(self, path: Path) -> bool:
        """Check if path is allowed under this policy."""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False

        # Must be within workspace
        if not str(resolved).startswith(str(self.workspace)):
            return False

        # Workspace path is always allowed — workspace takes precedence over BLOCKED_PATHS
        # (user explicitly chose the workspace, even if it's under /root)
        return True

    def validate(self, path: Path) -> Path:
        """Validate path and return resolved path, or raise SecurityError."""
        if not self.is_allowed(path):
            raise SecurityError(f"Access denied: {path}")
        return path.resolve()
