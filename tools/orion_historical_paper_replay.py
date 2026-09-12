from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BINANS_SCANNER = ROOT / "binansScanner"
if str(BINANS_SCANNER) not in sys.path:
    sys.path.insert(0, str(BINANS_SCANNER))

from replay.dataset import HistoricalDataset
from replay.runner import HistoricalPaperReplayRunner, ReplayConfig


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run ORION Historical Paper Replay")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--campaign", choices=("7D", "30D", "90D", "365D"), default="7D")
    parser.add_argument("--acceleration-factor", type=float, default=600.0)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--broad-pool-top-n", type=int, default=100)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    dataset = HistoricalDataset.from_directory(args.dataset)
    replay_config = ReplayConfig(
        campaign=args.campaign,
        acceleration_factor=args.acceleration_factor,
        active_top_n=args.top_n,
        broad_pool_top_n=args.broad_pool_top_n,
    )
    runner = HistoricalPaperReplayRunner.build(
        dataset,
        args.output_dir,
        replay_config=replay_config,
    )
    report = asyncio.run(runner.run_replay(dataset, replay_config=replay_config))
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
