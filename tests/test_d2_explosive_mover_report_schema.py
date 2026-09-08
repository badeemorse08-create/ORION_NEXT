from __future__ import annotations

import json
from pathlib import Path


EXPECTED = {
    "universe_completeness": "NOT_ESTABLISHED",
    "production_semantic_impact": "NONE",
    "current_exchange_info_used": False,
    "future_universe_information_used": False,
    "campaign_B": "BLOCKED",
    "broad_market_complete_universe_test": "BLOCKED",
}


def test_machine_report_schema_contract_matches_expected_values() -> None:
    from tools.explosive_mover_challenge import REPORT_VALIDATION_CONTRACT

    assert REPORT_VALIDATION_CONTRACT == EXPECTED
    assert set(REPORT_VALIDATION_CONTRACT) == set(EXPECTED)

    round_tripped = json.loads(json.dumps(REPORT_VALIDATION_CONTRACT, sort_keys=True))
    assert round_tripped == EXPECTED
    assert type(round_tripped["current_exchange_info_used"]) is bool
    assert type(round_tripped["future_universe_information_used"]) is bool


def test_source_defines_canonical_report_contract() -> None:
    source = Path("tools/explosive_mover_challenge.py").read_text(encoding="utf-8")
    assert "REPORT_VALIDATION_CONTRACT =" in source
    assert "_validate_report_contract(report)" in source
    assert "_validate_report_contract(json.loads" in source
