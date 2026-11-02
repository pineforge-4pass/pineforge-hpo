from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import tempfile
import unittest

from pineforge_hpo.cli import CliError, _validate_manifest_inputs
from pineforge_hpo.study_spec import ParameterSpec, StudySpecError, load_study_spec
from pineforge_hpo.symbol_bindings import (
    DatasetDeclaration,
    SymbolBinding,
    SymbolFacts,
    validate_bound_policy,
    validate_symbol_bindings,
)


class SymbolBindingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = self.dataset("first", "asset-a")
        self.second = self.dataset("second", "asset-b")
        self.datasets = (self.second, self.first)
        self.domain = ParameterSpec(kind="categorical", choices=("asset-a", "asset-b"))
        self.binding = SymbolBinding(
            input_title="Market",
            role="chart",
            datasets=(("asset-b", "second"), ("asset-a", "first")),
        )

    @staticmethod
    def dataset(dataset_id: str, symbol: str) -> DatasetDeclaration:
        return DatasetDeclaration(
            id=dataset_id,
            symbol=symbol,
            input_tf="60",
            script_tf="60",
            chart_timezone="UTC",
            syminfo=SymbolFacts(
                tickerid=symbol,
                type="synthetic",
                currency="units",
                mintick=0.5,
                pointvalue=1.0,
                mincontract=1.0,
                timezone="UTC",
                session="24x7",
            ),
        )

    def validate(self, *, bindings=None, domain=None, datasets=None, references=None):
        return validate_symbol_bindings(
            (self.binding,) if bindings is None else bindings,
            search_space={"Market": self.domain if domain is None else domain},
            referenced_dataset_ids=("second", "first")
            if references is None
            else references,
            datasets=self.datasets if datasets is None else datasets,
        )

    def assert_refusal(self, reason: str, field: str, function) -> None:
        with self.assertRaises(CliError) as raised:
            function()
        error = raised.exception
        self.assertEqual(error.code, "hpo_study_spec_invalid")
        self.assertEqual(error.failure_args, {"reason": reason})
        self.assertIn(field, str(error))

    def test_total_domain_follows_choice_order(self) -> None:
        resolved = self.validate()
        self.assertEqual(resolved[0].choices, ("asset-a", "asset-b"))
        self.assertEqual(
            tuple(item.id for item in resolved[0].datasets), ("first", "second")
        )
        self.assertEqual(resolved[0].datasets[0].syminfo, self.first.syminfo)
        with self.assertRaises(FrozenInstanceError):
            resolved[0].role = "request"

    def test_request_role_can_accompany_chart_role(self) -> None:
        request = replace(self.binding, input_title="Reference", role="request")
        resolved = validate_symbol_bindings(
            (self.binding, request),
            search_space={"Market": self.domain, "Reference": self.domain},
            referenced_dataset_ids=("second", "first"),
            datasets=self.datasets,
        )
        self.assertEqual(tuple(item.role for item in resolved), ("chart", "request"))
        self.assertEqual(resolved[1].datasets[0].id, "first")

    def test_incomplete_domain_is_refused(self) -> None:
        binding = replace(self.binding, datasets=(("asset-a", "first"),))
        self.assert_refusal(
            "input_symbol", "Market", lambda: self.validate(bindings=(binding,))
        )

    def test_extra_mapping_key_is_refused(self) -> None:
        binding = replace(
            self.binding, datasets=self.binding.datasets + (("asset-c", "first"),)
        )
        self.assert_refusal(
            "input_symbol", "datasets", lambda: self.validate(bindings=(binding,))
        )

    def test_non_string_choice_is_refused(self) -> None:
        for value in (1, 1.0, True, None):
            with self.subTest(value=value):
                domain = replace(self.domain, choices=("asset-a", value))
                self.assert_refusal(
                    "input_symbol", "choices", lambda: self.validate(domain=domain)
                )

    def test_non_categorical_domain_is_refused(self) -> None:
        self.assert_refusal(
            "input_symbol",
            "Market",
            lambda: self.validate(domain=ParameterSpec(kind="integer", low=1, high=2)),
        )

    def test_missing_search_input_is_refused(self) -> None:
        binding = replace(self.binding, input_title="Missing")
        self.assert_refusal(
            "input_symbol", "Missing", lambda: self.validate(bindings=(binding,))
        )

    def test_duplicate_choice_is_refused(self) -> None:
        domain = replace(self.domain, choices=("asset-a", "asset-a"))
        self.assert_refusal(
            "input_symbol", "choices", lambda: self.validate(domain=domain)
        )

    def test_duplicate_mapping_key_is_refused(self) -> None:
        binding = replace(
            self.binding, datasets=self.binding.datasets + (("asset-a", "first"),)
        )
        self.assert_refusal(
            "input_symbol", "duplicate", lambda: self.validate(bindings=(binding,))
        )

    def test_non_string_mapping_key_is_refused(self) -> None:
        binding = replace(
            self.binding, datasets=((False, "second"), ("asset-a", "first"))
        )
        self.assert_refusal(
            "input_symbol", "datasets", lambda: self.validate(bindings=(binding,))
        )

    def test_duplicate_dataset_id_is_refused(self) -> None:
        duplicate = replace(self.second, id="first")
        self.assert_refusal(
            "input_symbol",
            "first",
            lambda: self.validate(datasets=(self.first, duplicate)),
        )

    def test_duplicate_reference_id_is_refused(self) -> None:
        self.assert_refusal(
            "input_symbol",
            "duplicates",
            lambda: self.validate(references=("first", "second", "first")),
        )

    def test_duplicate_mapping_target_is_refused(self) -> None:
        binding = replace(
            self.binding, datasets=(("asset-a", "first"), ("asset-b", "first"))
        )
        self.assert_refusal(
            "input_symbol", "first", lambda: self.validate(bindings=(binding,))
        )

    def test_duplicate_binding_input_is_refused(self) -> None:
        self.assert_refusal(
            "input_symbol",
            "Market",
            lambda: self.validate(bindings=(self.binding, self.binding)),
        )

    def test_unknown_dataset_is_refused(self) -> None:
        binding = replace(
            self.binding, datasets=(("asset-a", "first"), ("asset-b", "missing"))
        )
        self.assert_refusal(
            "input_symbol", "missing", lambda: self.validate(bindings=(binding,))
        )

    def test_unknown_reference_is_refused(self) -> None:
        self.assert_refusal(
            "input_symbol",
            "missing",
            lambda: self.validate(references=("first", "second", "missing")),
        )

    def test_unreferenced_dataset_is_refused(self) -> None:
        self.assert_refusal(
            "input_symbol", "second", lambda: self.validate(references=("first",))
        )

    def test_declared_symbol_mismatch_is_refused(self) -> None:
        other = self.dataset("second", "asset-c")
        self.assert_refusal(
            "input_symbol",
            "asset-b",
            lambda: self.validate(datasets=(self.first, other)),
        )

    def test_fact_ticker_mismatch_is_refused(self) -> None:
        other = replace(
            self.second, syminfo=replace(self.second.syminfo, tickerid="asset-c")
        )
        self.assert_refusal(
            "input_symbol",
            "tickerid",
            lambda: self.validate(datasets=(self.first, other)),
        )

    def test_multiple_chart_roles_are_refused(self) -> None:
        other = replace(self.binding, input_title="Reference")
        self.assert_refusal(
            "input_symbol",
            "chart",
            lambda: validate_symbol_bindings(
                (self.binding, other),
                search_space={"Market": self.domain, "Reference": self.domain},
                referenced_dataset_ids=("first", "second"),
                datasets=self.datasets,
            ),
        )

    def test_missing_chart_role_is_refused(self) -> None:
        request = replace(self.binding, role="request")
        self.assert_refusal(
            "input_symbol", "chart", lambda: self.validate(bindings=(request,))
        )

    def test_unknown_role_is_refused(self) -> None:
        binding = replace(self.binding, role="other")
        self.assert_refusal(
            "input_symbol", "role", lambda: self.validate(bindings=(binding,))
        )

    def test_timeframes_are_canonicalized_and_invalid_forms_refused(self) -> None:
        daily = replace(self.first, input_tf="D", script_tf="1D")
        resolved = self.validate(datasets=(daily, self.second))
        self.assertEqual(resolved[0].datasets[0].input_tf, "1D")
        for field in ("input_tf", "script_tf"):
            with self.subTest(field=field):
                invalid = replace(self.first, **{field: "invalid"})
                self.assert_refusal(
                    "backtest",
                    field,
                    lambda: self.validate(datasets=(invalid, self.second)),
                )

    def test_timezone_mismatch_is_refused(self) -> None:
        invalid = replace(self.first, chart_timezone="Etc/GMT")
        self.assert_refusal(
            "backtest",
            "timezone",
            lambda: self.validate(datasets=(invalid, self.second)),
        )

    def test_incomplete_or_invalid_facts_are_refused(self) -> None:
        for field, value in (
            ("currency", ""),
            ("session", ""),
            ("timezone", ""),
            ("type", ""),
            ("mintick", 0),
            ("pointvalue", float("nan")),
            ("mincontract", True),
        ):
            with self.subTest(field=field):
                invalid = replace(
                    self.first, syminfo=replace(self.first.syminfo, **{field: value})
                )
                self.assert_refusal(
                    "backtest",
                    field,
                    lambda: self.validate(datasets=(invalid, self.second)),
                )

    def test_unreferenced_declarations_are_still_validated(self) -> None:
        extra = replace(self.dataset("extra", "asset-c"), script_tf="")
        self.assert_refusal(
            "backtest",
            "script_tf",
            lambda: self.validate(datasets=self.datasets + (extra,)),
        )

    def test_valid_extra_declaration_does_not_expand_the_binding(self) -> None:
        extra = self.dataset("extra", "asset-c")
        resolved = self.validate(datasets=self.datasets + (extra,))
        self.assertEqual(
            tuple(item.id for item in resolved[0].datasets), ("first", "second")
        )

    def test_explicit_policy_approval_is_required(self) -> None:
        resolved = self.validate()
        self.assertIsNone(
            validate_bound_policy(
                resolved,
                pruner="none",
                alignment_mode="exact_timestamps",
                approved_alignment_modes={"exact_timestamps"},
            )
        )
        self.assert_refusal(
            "backtest",
            "data_comparison.alignment",
            lambda: validate_bound_policy(
                resolved,
                pruner="none",
                alignment_mode="exact_timestamps",
                approved_alignment_modes=set(),
            ),
        )
        self.assert_refusal(
            "backtest",
            "data_comparison.alignment",
            lambda: validate_bound_policy(
                resolved,
                pruner="none",
                alignment_mode="per_symbol_calendar",
                approved_alignment_modes={"exact_timestamps"},
            ),
        )

    def test_undecided_pruner_policy_is_refused(self) -> None:
        resolved = self.validate()
        self.assert_refusal(
            "pruner",
            "execution.pruner",
            lambda: validate_bound_policy(
                resolved,
                pruner="successive_halving",
                alignment_mode="exact_timestamps",
                approved_alignment_modes={"exact_timestamps"},
            ),
        )

    def test_undecided_quote_currency_policy_is_refused(self) -> None:
        other = replace(
            self.second, syminfo=replace(self.second.syminfo, currency="other-units")
        )
        resolved = self.validate(datasets=(self.first, other))
        self.assert_refusal(
            "objective",
            "other-units",
            lambda: validate_bound_policy(
                resolved,
                pruner="none",
                alignment_mode="exact_timestamps",
                approved_alignment_modes={"exact_timestamps"},
            ),
        )

    @staticmethod
    def public_document() -> dict:
        return {
            "schema_version": 1,
            "mode": "single_strategy",
            "strategies": [
                {
                    "id": "strategy",
                    "source": "strategy.pine",
                    "datasets": ["chart"],
                    "search_space": {
                        "Market": {
                            "kind": "categorical",
                            "choices": ["asset-a", "asset-b"],
                        }
                    },
                }
            ],
            "datasets": [{"id": "chart", "ohlcv": "bars.csv", "input_tf": "60"}],
            "objective": {
                "kind": "expression",
                "direction": "maximize",
                "expression": "metrics.all.net_profit",
            },
            "sampler": {"kind": "grid", "seed": 17, "trials": 2},
            "execution": {"workers": 1, "isolation": "threads", "pruner": "none"},
        }

    def test_public_schema_still_rejects_symbol_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            document = self.public_document()
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(load_study_spec(path).strategy.id, "strategy")
            document["strategies"][0]["symbol_bindings"] = {
                "Market": {
                    "role": "chart",
                    "datasets": {
                        "asset-a": "first",
                        "asset-b": "second",
                    },
                },
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                StudySpecError, "symbol_bindings.*unknown field"
            ) as raised:
                load_study_spec(path)
            self.assertEqual(raised.exception.code, "hpo_study_spec_invalid")
            self.assertEqual(raised.exception.failure_args, {"reason": "study"})

    def test_public_manifest_gate_still_refuses_searched_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            path.write_text(json.dumps(self.public_document()), encoding="utf-8")
            study = load_study_spec(path)
            for definition in (
                {"type": "string", "kind": "symbol"},
                {"type": "symbol"},
            ):
                with self.subTest(definition=definition):
                    with self.assertRaisesRegex(
                        CliError, "input.symbol.*refused.*D7"
                    ) as raised:
                        _validate_manifest_inputs(
                            study,
                            [{"title": "Market", **definition}],
                            input_kinds_emitted=True,
                        )
                    self.assertEqual(raised.exception.code, "hpo_study_spec_invalid")
                    self.assertEqual(
                        raised.exception.failure_args, {"reason": "artifact_metadata"}
                    )


if __name__ == "__main__":
    unittest.main()
