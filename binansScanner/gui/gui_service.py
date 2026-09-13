"""
===============================================================================
Badee Binance Scanner
Architecture : ORION
Module       : gui.gui_service
Version      : 1.0.0
Status       : ORION Production V1.0 INITIAL
===============================================================================

GUI Service layer managing user interface state, notifications, and actions
completely decoupled from any graphical framework. Trading controls delegate
to the persistent runtime control boundary; GUI state is never authoritative.
===============================================================================
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from gui.gui_models import GuiAction, GuiNotification, GuiResult, GuiState
from integration.trading_control import TradingControlStore, TradingState

base_logger = logging.getLogger(__name__)


class GuiServiceError(Exception):
    """Base exception class for all GUI service related errors."""
    pass


class LoggerAdapter(logging.LoggerAdapter):
    """Custom LoggerAdapter injecting GUI service context attributes into log entries."""

    def process(self, msg: str, kwargs: Any) -> tuple[str, dict[str, Any]]:
        context = self.extra or {}
        context_str = " | ".join(f"{k}={v}" for k, v in context.items() if v is not None)
        formatted_msg = f"[{context_str}] {msg}" if context_str else msg
        return formatted_msg, kwargs


class GuiService:
    """Service layer for presentation state and canonical trading controls."""

    def __init__(self, logger: Optional[logging.Logger] = None, trading_control: Optional[TradingControlStore] = None) -> None:
        self._logger_instance = logger if logger is not None else base_logger
        self._logger = LoggerAdapter(self._logger_instance, {"component": "GuiService", "operation": "init"})
        self._state: GuiState = GuiState(connected=False, running=False, busy=False, metadata={})
        self._notifications: list[GuiNotification] = []
        self._actions: dict[str, GuiAction] = {}
        self._trading_control = trading_control or TradingControlStore(Path.home() / ".orion" / "trading_control.json")
        self._trading_control.initialize()
        self._logger.info("GuiService initialized successfully.")

    def _get_logger(self, operation: Optional[str] = None) -> LoggerAdapter:
        return LoggerAdapter(self._logger_instance, {"component": "GuiService", "operation": operation})

    def state(self) -> GuiState:
        return self._state

    def set_state(self, state: GuiState) -> None:
        self._get_logger("set_state").info("Updating GUI state snapshot.")
        self._state = state

    def add_notification(self, notification: GuiNotification) -> None:
        self._get_logger("add_notification").info(f"Adding notification with title '{notification.title}'.")
        self._notifications.append(notification)

    def notifications(self) -> tuple[GuiNotification, ...]:
        return tuple(self._notifications)

    def clear_notifications(self) -> None:
        self._get_logger("clear_notifications").info("Clearing all GUI notifications.")
        self._notifications.clear()

    def register_action(self, action: GuiAction) -> None:
        logger = self._get_logger("register_action")
        if not action.name:
            raise GuiServiceError("Cannot register GUI action with an empty name.")
        if action.name in self._actions:
            raise GuiServiceError(f"GUI action '{action.name}' is already registered.")
        self._actions[action.name] = action
        logger.info(f"GUI action '{action.name}' registered successfully.")

    def actions(self) -> tuple[GuiAction, ...]:
        return tuple(self._actions.values())

    def trading_state(self) -> TradingState:
        return self._trading_control.state

    def trading_actions(self) -> tuple[GuiAction, ...]:
        state = self.trading_state()
        return (
            GuiAction("PAUSE NEW ENTRIES", enabled=state is TradingState.RUNNING, metadata={"control": "pause_new_entries", "state": state.value}),
            GuiAction("RESUME TRADING", enabled=state is TradingState.PAUSED, metadata={"control": "resume_trading", "state": state.value}),
        )

    def pause_new_entries(self, *, source: str = "ui", reason: str = "user clicked pause") -> TradingState:
        state = self._trading_control.pause(source=source, reason=reason)
        self._logger.info("New entries paused by canonical runtime control.")
        return state

    def resume_trading(self, *, source: str = "ui", reason: str = "user clicked resume") -> TradingState:
        state = self._trading_control.resume(source=source, reason=reason)
        self._logger.info("Trading resumed by canonical runtime control.")
        return state

    def execute(self) -> GuiResult:
        return GuiResult(success=True, message="GUI service executed successfully.", metadata={"trading_state": self.trading_state().value})


# =============================================================================
# End Of File
# =============================================================================
