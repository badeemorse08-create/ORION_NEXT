"""
===============================================================================
Badee Binance Scanner
Architecture : ORION
Module       : gui.gui_window
Version      : 1.0.0
Status       : ORION Production V1.0 INITIAL
===============================================================================

Abstract GUI Window shell representing the main presentation layer entry point.
The trading buttons are presentation bindings only; runtime control remains
owned by the persistent PaperRuntimeSupervisor boundary.
===============================================================================
"""

from __future__ import annotations

from gui.gui_controller import GuiController
from gui.gui_models import GuiAction
from integration.trading_control import TradingState


class GuiWindow:
    """Abstract GUI window shell with canonical trading-control bindings."""

    def __init__(self, controller: GuiController) -> None:
        self._controller = controller
        self._initialized: bool = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True

    def initialized(self) -> bool:
        return self._initialized

    def show(self) -> None:
        pass

    def close(self) -> None:
        pass

    def controller(self) -> GuiController:
        return self._controller

    def trading_controls(self) -> tuple[GuiAction, ...]:
        """Return the two UI buttons with enabled state derived from runtime."""
        return self._controller.trading_actions()

    def pause_new_entries(self) -> TradingState:
        return self._controller.pause_new_entries()

    def resume_trading(self) -> TradingState:
        return self._controller.resume_trading()


# =============================================================================
# End Of File
# =============================================================================
