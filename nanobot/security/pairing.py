"""Device pairing manager for secure connections."""

from __future__ import annotations

import secrets
import time
from typing import Optional


class PairingError(Exception):
    """Raised when pairing verification fails."""
    pass


class PairingManager:
    """Pairing manager - 6-digit one-time code for secure device pairing.

    Usage:
        manager = PairingManager()

        # Host generates a code for a device
        code = manager.generate_code("device-123")
        # Show code to user on device

        # Device submits code for verification
        if manager.verify("device-123", "123456"):
            # Pairing successful, register device
            manager.register("device-123", user_id="jack")
    """

    DEFAULT_EXPIRY_SECONDS = 300  # 5 minutes

    def __init__(self, expiry_seconds: int = DEFAULT_EXPIRY_SECONDS):
        self._expiry_seconds = expiry_seconds
        self._pending: dict[str, dict] = {}  # device_id -> {code, expires_at}
        self._registered: dict[str, dict] = {}  # device_id -> {user_id, paired_at}

    def generate_code(self, device_id: str) -> str:
        """Generate a 6-digit pairing code for the device.

        The code expires after expiry_seconds (default 5 minutes).
        """
        code = "".join(str(secrets.randbelow(10)) for _ in range(6))
        self._pending[device_id] = {
            "code": code,
            "expires_at": time.time() + self._expiry_seconds,
        }
        return code

    def verify(self, device_id: str, code: str) -> bool:
        """Verify a pairing code submitted by the device.

        Returns True if code is valid and not expired.
        The code is consumed on successful verification.
        """
        info = self._pending.pop(device_id, None)
        if not info:
            return False

        if time.time() > info["expires_at"]:
            return False

        if info["code"] != code:
            return False

        return True

    def register(self, device_id: str, user_id: str) -> None:
        """Register a successfully paired device."""
        self._registered[device_id] = {
            "user_id": user_id,
            "paired_at": time.time(),
        }

    def is_registered(self, device_id: str) -> bool:
        """Check if a device is registered."""
        return device_id in self._registered

    def get_user(self, device_id: str) -> Optional[str]:
        """Get the user ID for a registered device."""
        info = self._registered.get(device_id)
        return info["user_id"] if info else None

    def revoke(self, device_id: str) -> bool:
        """Revoke a device pairing. Returns True if device was registered."""
        if device_id in self._registered:
            del self._registered[device_id]
            return True
        return False

    def cleanup_expired(self) -> int:
        """Remove expired pending codes. Returns count removed."""
        now = time.time()
        expired = [
            did for did, info in self._pending.items()
            if now > info["expires_at"]
        ]
        for did in expired:
            del self._pending[did]
        return len(expired)
