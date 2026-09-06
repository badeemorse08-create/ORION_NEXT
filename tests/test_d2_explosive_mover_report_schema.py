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


def _report_literal_values(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "build_evidence":
            continue
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Assign):
                continue
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                continue
            if statement.targets[0].id != "report" or not isinstance(statement.value, ast.Dict):
                continue
            values: dict[str, object] = {}
            for key_node, value_node in zip(statement.value.keys, statement.value.values):
                if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                    continue
                try:
                    values[key_node.value] = ast.literal_eval(value_node)
                except (ValueError, TypeError):
                    continue
            return values
    raise AssertionError("build_evidence report dict was not found")


def test_machine_report_schema_matches_validator_contract() -> None:
    source = Path("tools/explosive_mover_challenge.py").read_text(encoding="utf-8")
    actual = {key: _report_literal_values(source).get(key) for key in EXPECTED}

    assert actual == EXPECTED

    round_tripped = json.loads(json.dumps(actual, sort_keys=True))
    assert round_tripped == EXPECTED
    assert round_tripped["current_exchange_info_used"] is False
    assert round_tripped["future_universe_information_used"] is False
