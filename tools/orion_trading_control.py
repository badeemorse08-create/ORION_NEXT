"""Small operator UI for the canonical runtime trading-control boundary."""
from __future__ import annotations

import argparse
from pathlib import Path

from integration.trading_control import TradingControlStore


def _default_state_file() -> Path:
    return Path.home() / ".orion" / "trading_control.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ORION persistent new-entry trading control")
    parser.add_argument("action", choices=("status", "pause", "resume"))
    parser.add_argument("--state-file", type=Path, default=_default_state_file())
    parser.add_argument("--source", default="operator-ui")
    parser.add_argument("--reason", default="operator request")
    args = parser.parse_args(argv)
    control = TradingControlStore(args.state_file)
    if args.action == "pause":
        state = control.pause(source=args.source, reason=args.reason)
    elif args.action == "resume":
        state = control.resume(source=args.source, reason=args.reason)
    else:
        state = control.state
    print(f"TRADING_STATE={state.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
