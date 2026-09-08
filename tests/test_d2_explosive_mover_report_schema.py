from __future__ import annotations

import ast
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


def _source_contract() -> dict:
    source = Path("tools/explosive_mover_challenge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "REPORT_VALIDATION_CONTRACT"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, dict)
            return value
    raise AssertionError("REPORT_VALIDATION_CONTRACT assignment not found")


def test_machine_report_schema_contract_matches_expected_values() -> None:
    contract = _source_contract()
    assert contract == EXPECTED
    assert set(contract) == set(EXPECTED)

    round_tripped = json.loads(json.dumps(contract, sort_keys=True))
    assert round_tripped == EXPECTED
    assert type(round_tripped["current_exchange_info_used"]) is bool
    assert type(round_tripped["future_universe_information_used"]) is bool


def test_source_defines_canonical_report_contract_and_validates_round_trip() -> None:
    source = Path("tools/explosive_mover_challenge.py").read_text(encoding="utf-8")
    assert "REPORT_VALIDATION_CONTRACT =" in source
    assert "_validate_report_contract(report)" in source
    assert "_validate_report_contract(json.loads" in source
