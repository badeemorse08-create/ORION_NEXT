"""
===============================================================================
Badee Binance Scanner
Architecture : ORION
Module       : gui.gui_controller
Version      : 1.0.0
Status       : ORION Production V1.0 INITIAL
===============================================================================

Thin GUI Controller layer responsible solely for delegating requests and state
queries directly to the underlying GuiService. Trading controls are delegated
to the canonical persistent runtime boundary and never stored in GUI state.
===============================================================================
"""

from __future__ import annotations

from gui.gui_models import GuiAction, GuiNotification, GuiResult, GuiState
from gui.gui_service import GuiService
from integration.trading_control import TradingState


class GuiController:
    """Thin proxy controller over GuiService."""

    def __init__(self, service: GuiService) -> None:
        self._service = service

    def state(self) -> GuiState:
        return self._service.state()

    def update_state(self, state: GuiState) -> None:
        self._service.set_state(state)

    def notify(self, notification: GuiNotification) -> None:
        self._service.add_notification(notification)

    def notifications(self) -> tuple[GuiNotification, ...]:
        return self._service.notifications()

    def clear_notifications(self) -> None:
        self._service.clear_notifications()

    def execute(self) -> GuiResult:
        return self._service.execute()

    def trading_state(self) -> TradingState:
        return self._service.trading_state()

    def trading_actions(self) -> tuple[GuiAction, ...]:
        return self._service.trading_actions()

    def pause_new_entries(self) -> TradingState:
        return self._service.pause_new_entries()

    def resume_trading(self) -> TradingState:
        return self._service.resume_trading()


# =============================================================================
# End Of File
# =============================================================================
