from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FaultState:
    slow_page: bool = False
    slow_page_delay_sec: float = 2.0
    slow_page_paths: list[str] | None = None  # None means all paths, or list of path prefixes

    renamed_elements: bool = False  # UI drift in ERP forms

    session_expiry_mid_task: bool = False  # Expires session on next authenticated request
    session_expiry_target: str = "erp"  # "erp", "portal", "tickets", or "any"

    transient_500: bool = False  # Fails once with 500, then resets
    transient_500_count: int = 1
    transient_500_path: str = ""  # Specific path or empty for any

    duplicate_invoice_rejection: bool = True  # Standard duplicate prevention in ERP

    missing_portal_invoice: bool = False  # Hides specific invoice from portal
    missing_invoice_number: str = "INV-GLOBEX-901"

    ocr_noise_mismatch: bool = False  # Emits noisy or mismatched text if requested


class FaultManager:
    """
    Thread-safe runtime fault injection manager.
    Can be dynamically toggled via /__truth/faults API without restarting the server.
    """

    def __init__(self) -> None:
        self.state = FaultState()

    def get_faults(self) -> dict[str, Any]:
        return asdict(self.state)

    def update_faults(self, updates: dict[str, Any]) -> dict[str, Any]:
        for k, v in updates.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
                logger.info(f"Fault updated: {k} = {v}")
        return self.get_faults()

    def reset_faults(self) -> dict[str, Any]:
        self.state = FaultState()
        logger.info("All dynamic faults reset to default.")
        return self.get_faults()

    async def maybe_delay(self, path: str) -> None:
        """Trigger artificial delay if slow_page fault is active."""
        if not self.state.slow_page:
            return
        if self.state.slow_page_paths:
            if any(p in path for p in self.state.slow_page_paths):
                logger.warning(
                    f"Fault active: injecting {self.state.slow_page_delay_sec}s delay on {path}"
                )
                await asyncio.sleep(self.state.slow_page_delay_sec)
        else:
            logger.warning(
                f"Fault active: injecting {self.state.slow_page_delay_sec}s delay on {path}"
            )
            await asyncio.sleep(self.state.slow_page_delay_sec)

    def should_trigger_500(self, path: str) -> bool:
        """Check if transient 500 should fire, and decrement counter."""
        if not self.state.transient_500 or self.state.transient_500_count <= 0:
            return False
        if self.state.transient_500_path and self.state.transient_500_path not in path:
            return False

        self.state.transient_500_count -= 1
        if self.state.transient_500_count == 0:
            self.state.transient_500 = False
        logger.warning(
            f"Fault active: triggering transient 500 on {path}. Remaining: {self.state.transient_500_count}"
        )
        return True

    def should_expire_session(self, system: str) -> bool:
        """Check if session expiry should fire, and reset flag."""
        if not self.state.session_expiry_mid_task:
            return False
        if self.state.session_expiry_target in ("any", system):
            self.state.session_expiry_mid_task = False
            logger.warning(f"Fault active: expiring session mid-task for system '{system}'")
            return True
        return False


# Global singleton fault manager
fault_manager = FaultManager()
