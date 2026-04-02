"""Platform detection utilities for multi-platform support."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass


@dataclass
class PlatformInfo:
    """Platform information."""
    arch: str           # x86_64, arm64, riscv, unknown
    os: str             # linux, darwin, windows
    is_low_resource: bool  # <512MB RAM
    python_version: str

    @property
    def is_arm(self) -> bool:
        return self.arch in ("arm64", "aarch64")

    @property
    def is_x86(self) -> bool:
        return self.arch in ("x86_64", "amd64")


class PlatformDetector:
    """Platform detection and optimization flags."""

    # Architecture mapping
    _ARCH_MAP = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }

    @staticmethod
    def get_arch() -> str:
        """Get normalized architecture string."""
        raw = platform.machine().lower()
        return PlatformDetector._ARCH_MAP.get(raw, "unknown")

    @staticmethod
    def get_os() -> str:
        """Get normalized OS string."""
        return platform.system().lower()

    @staticmethod
    def is_low_resource() -> bool:
        """Check if running on low-resource environment (<512MB RAM)."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            return mem.total < 512 * 1024 * 1024
        except ImportError:
            # Fallback: check available memory via os
            try:
                import os
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            return kb * 1024 < 512 * 1024 * 1024
            except Exception:
                pass
            return False

    @staticmethod
    def get_optimization_flags() -> dict:
        """Get platform-specific optimization flags."""
        is_low = PlatformDetector.is_low_resource()

        return {
            "arch": PlatformDetector.get_arch(),
            "os": PlatformDetector.get_os(),
            "low_resource": is_low,
            "batch_size": 5 if is_low else 10,
            "max_concurrent": 2 if is_low else 5,
            "connection_pool_size": 3 if is_low else 10,
        }

    @classmethod
    def detect(cls) -> PlatformInfo:
        """Detect and return full platform information."""
        return PlatformInfo(
            arch=cls.get_arch(),
            os=cls.get_os(),
            is_low_resource=cls.is_low_resource(),
            python_version=sys.version,
        )
