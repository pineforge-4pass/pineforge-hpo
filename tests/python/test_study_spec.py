from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo.study_spec import StudySpecError, load_study_spec  # noqa: E402


def valid_document() -> dict:
    return {
        "schema_version": 1,
        "mode": "single_strategy",
        "strategies": [
            {
                "id": "trend",
                "source": "strategies/trend.pine",
                "datasets": ["eth-1h"],
                "fixed_inputs": {"Direction": "long"},
                "strategy_overrides": {"initial_capital": 100000},
                "search_space": {
                    "Length": {"kind": "integer", "low": 2, "high": 50, "step": 1},
                    "Multiplier": {"kind": "real", "low": 0.5, "high": 4.0},
                    "Mode": {"kind": "categorical", "choices": ["ema", "sma"]},
                },
            }
        ],
        "datasets": [
            {
                "id": "eth-1h",
                "ohlcv": "data/ETH.csv",
                "input_tf": "60",
                "script_tf": "60",
                "chart_timezone": "UTC",
            }
        ],
        "objective": {
            "kind": "expression",
            "direction": "maximize",
            "expression": "metrics.all.net_profit_pct - metrics.equity.max_drawdown_pct",
            "constraints": ["metrics.all.num_trades >= 30"],
            "nan_policy": "fail_trial",
            "division_by_zero": "fail_trial",
        },
        "sampler": {"kind": "random", "seed": 1234, "trials": 100},
        "execution": {"workers": 4, "isolation": "threads"},
    }


class StudySpecTest(unittest.TestCase):
    def test_lagged_tpe_requires_constant_liar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["sampler"] = {
                "kind": "tpe",
                "seed": 1234,
                "trials": 100,
                "config": {"constant_liar": False},
            }
            document["execution"]["batch_lag"] = 1
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(StudySpecError, "batch_lag.*constant_liar"):
                load_study_spec(path)
            document["execution"]["batch_lag"] = 0
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertFalse(load_study_spec(path).sampler.config.constant_liar)
            document["execution"]["batch_lag"] = 1
            document["sampler"]["config"]["constant_liar"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertTrue(load_study_spec(path).sampler.config.constant_liar)

    def test_batching_and_pruning_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["execution"].update(
                batch_size=16,
                batch_lag=1,
                pruner="halving",
                pruner_rungs=[0.25, 0.5],
                pruner_eta=3,
            )
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            spec = load_study_spec(path)
            self.assertEqual(spec.execution.batch_size, 16)
            self.assertEqual(spec.execution.batch_lag, 1)
            self.assertEqual(spec.execution.pruner, "halving")
            self.assertEqual(spec.execution.pruner_rungs, (0.25, 0.5))
            self.assertEqual(spec.execution.pruner_eta, 3)
            for field, value in (
                ("batch_size", 0),
                ("batch_size", True),
                ("batch_lag", 2),
                ("pruner", "bad"),
                ("pruner_rungs", [0.5, 0.25]),
                ("pruner_eta", 1),
            ):
                with self.subTest(field=field):
                    broken = valid_document()
                    broken["execution"][field] = value
                    path.write_text(json.dumps(broken), encoding="utf-8")
                    with self.assertRaises(StudySpecError):
                        load_study_spec(path)

    def test_valid_single_strategy_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "strategies").mkdir()
            (root / "data").mkdir()
            (root / "strategies" / "trend.pine").write_text(
                "//@version=6\n", encoding="utf-8"
            )
            (root / "data" / "ETH.csv").write_text(
                "time,open,high,low,close,volume\n", encoding="utf-8"
            )
            path = root / "study.json"
            path.write_text(json.dumps(valid_document()), encoding="utf-8")

            spec = load_study_spec(path, require_files=True)

            self.assertEqual(
                spec.strategy.source, (root / "strategies" / "trend.pine").resolve()
            )
            self.assertEqual(
                spec.datasets[0].ohlcv, (root / "data" / "ETH.csv").resolve()
            )
            self.assertEqual(spec.search_space["Length"].kind, "integer")
            self.assertEqual(spec.fixed_inputs, {"Direction": "long"})
            self.assertEqual(spec.overrides, {"initial_capital": 100000})
            self.assertEqual(spec.sampler.seed, 1234)
            self.assertEqual(spec.sampler.candidate_policy, "sampler_default")
            self.assertIsNone(spec.sampler.config)

    def test_rejects_fixed_and_tunable_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["strategies"][0]["fixed_inputs"]["Length"] = 14
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(StudySpecError) as caught:
                load_study_spec(path)
            self.assertIn("both fixed and tunable", str(caught.exception))

    def test_rejects_unknown_dataset_and_invalid_sampler_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["strategies"][0]["datasets"] = ["missing"]
            document["sampler"]["seed"] = -1
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(StudySpecError) as caught:
                load_study_spec(path)
            message = str(caught.exception)
            self.assertIn("unknown dataset ids: missing", message)
            self.assertIn("non-negative integer", message)

    def test_rejects_unknown_sampler_nonportable_seed_and_ignored_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for sampler, expected in (
                (
                    {"kind": "typo", "seed": 1, "trials": 2},
                    "must be grid, random, dlib_global, tpe, candidates, or sobol",
                ),
                (
                    {
                        "kind": "dlib_global",
                        "seed": 2_147_483_648,
                        "trials": 2,
                    },
                    "must be <= 2147483647",
                ),
                (
                    {
                        "kind": "dlib_global",
                        "seed": 1,
                        "trials": 2,
                        "config": {"noise": 0.1},
                    },
                    "is reserved and must be empty",
                ),
            ):
                document = valid_document()
                document["sampler"] = sampler
                path = Path(temporary) / "study.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(sampler=sampler):
                    with self.assertRaisesRegex(StudySpecError, expected):
                        load_study_spec(path)

    def test_tpe_config_is_typed_and_defaults_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["sampler"] = {
                "kind": "tpe",
                "seed": 42,
                "trials": 200,
                "config": {
                    "startup_trials": 20,
                    "ei_candidates": 48,
                    "history_switch": 4096,
                    "max_threads": 8,
                    "gamma_fraction": 0.2,
                    "gamma_cap": 40,
                    "prior_weight": 1.5,
                    "constant_liar": False,
                },
            }
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            config = load_study_spec(path).sampler.config
            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.startup_trials, 20)
            self.assertEqual(config.ei_candidates, 48)
            self.assertEqual(config.history_switch, 4096)
            self.assertEqual(config.max_threads, 8)
            self.assertEqual(config.gamma_fraction, 0.2)
            self.assertEqual(config.gamma_cap, 40)
            self.assertEqual(config.prior_weight, 1.5)
            self.assertFalse(config.constant_liar)

            document["sampler"].pop("config")
            path.write_text(json.dumps(document), encoding="utf-8")
            defaults = load_study_spec(path).sampler.config
            self.assertIsNotNone(defaults)
            assert defaults is not None
            self.assertEqual(defaults.startup_trials, 10)
            self.assertEqual(defaults.ei_candidates, 24)
            self.assertIsNone(defaults.history_switch)
            self.assertEqual(defaults.max_threads, 0)
            self.assertEqual(defaults.gamma_fraction, 0.10)
            self.assertEqual(defaults.gamma_cap, 25)
            self.assertEqual(defaults.prior_weight, 1.0)
            self.assertTrue(defaults.constant_liar)
            document["sampler"]["config"] = {"history_switch": None}
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertIsNone(load_study_spec(path).sampler.config.history_switch)

    def test_tpe_config_rejects_unknown_fields_and_invalid_ranges(self) -> None:
        invalid = (
            ({"max_threads": -1}, r"max_threads: must be an integer in \[0, 1024\]"),
            ({"max_threads": True}, r"max_threads: must be an integer in \[0, 1024\]"),
            ({"max_threads": 1025}, r"max_threads: must be an integer in \[0, 1024\]"),
            ({"history_switch": 0}, "history_switch: must be a positive uint64"),
            ({"history_switch": True}, "history_switch: must be a positive uint64"),
            ({"history_switch": 2**64}, "history_switch: must be a positive uint64"),
            ({"unknown": 1}, "config.unknown: unknown field"),
            ({"startup_trials": 0}, "startup_trials: must be a positive integer"),
            (
                {"ei_candidates": True},
                "ei_candidates: must be an integer between 1 and 1000000",
            ),
            (
                {"ei_candidates": 1_000_001},
                "ei_candidates: must be an integer between 1 and 1000000",
            ),
            (
                {"gamma_fraction": 0},
                "gamma_fraction: must be a finite number greater than 0 and at most 1",
            ),
            (
                {"gamma_fraction": 1.1},
                "gamma_fraction: must be a finite number greater than 0 and at most 1",
            ),
            ({"gamma_cap": 0}, "gamma_cap: must be a positive integer"),
            (
                {"prior_weight": -1},
                "prior_weight: must be a finite number greater than 0",
            ),
            ({"constant_liar": 1}, "constant_liar: must be a boolean"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for config, expected in invalid:
                document = valid_document()
                document["sampler"] = {
                    "kind": "tpe",
                    "seed": 42,
                    "trials": 200,
                    "config": config,
                }
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(config=config):
                    with self.assertRaisesRegex(StudySpecError, expected):
                        load_study_spec(path)

    def test_finite_candidate_policies_accept_tpe_and_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for sampler_kind in ("tpe", "grid"):
                document = valid_document()
                document["strategies"][0]["search_space"]["Multiplier"]["step"] = 0.5
                document["sampler"] = {
                    "kind": sampler_kind,
                    "seed": 42,
                    "trials": 784,
                    "candidate_policy": "exhaustive",
                }
                path.write_text(json.dumps(document), encoding="utf-8")

                with self.subTest(sampler=sampler_kind):
                    sampler = load_study_spec(path).sampler
                    self.assertEqual(sampler.candidate_policy, "exhaustive")

            document = valid_document()
            document["strategies"][0]["search_space"]["Multiplier"]["step"] = 0.5
            document["sampler"] = {
                "kind": "tpe",
                "seed": 42,
                "trials": 100,
                "candidate_policy": "without_replacement",
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(
                load_study_spec(path).sampler.candidate_policy,
                "without_replacement",
            )

    def test_finite_candidate_policy_requires_quantized_varying_reals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["sampler"] = {
                "kind": "tpe",
                "seed": 42,
                "trials": 100,
                "candidate_policy": "without_replacement",
            }
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                StudySpecError,
                "step: is required for a varying real under a finite candidate policy",
            ):
                load_study_spec(path)

            document["strategies"][0]["search_space"]["Multiplier"]["log"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                StudySpecError,
                "non-constant log real is not supported by finite candidate policies",
            ):
                load_study_spec(path)

    def test_finite_candidate_policy_allows_fixed_unstepped_real(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            document["strategies"][0]["search_space"]["Multiplier"] = {
                "kind": "real",
                "low": 0.5,
                "high": 0.5,
                "log": True,
            }
            document["sampler"] = {
                "kind": "tpe",
                "seed": 42,
                "trials": 98,
                "candidate_policy": "exhaustive",
            }
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            sampler = load_study_spec(path).sampler
            self.assertEqual(sampler.candidate_policy, "exhaustive")

    def test_finite_real_cardinality_uses_endpoint_decoding(self) -> None:
        maximum = float.fromhex("0x1.fffffffffffffp+1023")
        cases = (
            (0.0, 0.3, 0.1, 4),
            (0.0, 1.0, 0.3, 4),
            # The former index-scaled tolerance incorrectly admitted the next
            # point at 1e12 even though it is many ULPs above the declared high.
            (0.0, 999_999_999_999.99, 1.0, 1_000_000_000_000),
            # nextafter(DBL_MAX, +inf) is inf; a non-finite decoded endpoint
            # still must not become a candidate.
            (0.0, maximum, maximum, 2),
            # high-low overflows binary64, but scaling each endpoint first
            # yields the exact three-point lattice {-DBL_MAX, 0, DBL_MAX}.
            (-maximum, maximum, maximum, 3),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for low, high, step, cardinality in cases:
                document = valid_document()
                document["strategies"][0]["search_space"] = {
                    "Value": {
                        "kind": "real",
                        "low": low,
                        "high": high,
                        "step": step,
                    }
                }
                document["sampler"] = {
                    "kind": "tpe",
                    "seed": 42,
                    "trials": cardinality,
                    "candidate_policy": "exhaustive",
                }
                path.write_text(json.dumps(document), encoding="utf-8")

                with self.subTest(low=low, high=high, step=step):
                    sampler = load_study_spec(path).sampler
                    self.assertEqual(sampler.trials, cardinality)
                    self.assertEqual(sampler.candidate_policy, "exhaustive")

    def test_finite_candidate_policy_validates_trial_budget(self) -> None:
        invalid = (
            (
                "without_replacement",
                785,
                "must not exceed finite search-space cardinality 784",
            ),
            (
                "exhaustive",
                783,
                "must equal finite search-space cardinality 784",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for policy, trials, expected in invalid:
                document = valid_document()
                document["strategies"][0]["search_space"]["Multiplier"]["step"] = 0.5
                document["sampler"] = {
                    "kind": "tpe",
                    "seed": 42,
                    "trials": trials,
                    "candidate_policy": policy,
                }
                path.write_text(json.dumps(document), encoding="utf-8")

                with self.subTest(policy=policy):
                    with self.assertRaisesRegex(StudySpecError, expected):
                        load_study_spec(path)

    def test_finite_candidate_policy_rejects_unsupported_sampler_and_overflow(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for sampler_kind in ("random", "dlib_global"):
                document = valid_document()
                document["strategies"][0]["search_space"]["Multiplier"]["step"] = 0.5
                document["sampler"] = {
                    "kind": sampler_kind,
                    "seed": 42,
                    "trials": 100,
                    "candidate_policy": "without_replacement",
                }
                path.write_text(json.dumps(document), encoding="utf-8")

                with self.subTest(sampler=sampler_kind):
                    with self.assertRaisesRegex(
                        StudySpecError,
                        "finite candidate policies are only supported by grid and tpe",
                    ):
                        load_study_spec(path)

            document = valid_document()
            document["strategies"][0]["search_space"] = {
                "First": {
                    "kind": "integer",
                    "low": 0,
                    "high": 4_294_967_295,
                },
                "Second": {
                    "kind": "integer",
                    "low": 0,
                    "high": 4_294_967_295,
                },
            }
            document["sampler"] = {
                "kind": "tpe",
                "seed": 42,
                "trials": 1,
                "candidate_policy": "without_replacement",
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                StudySpecError, "finite cardinality exceeds uint64"
            ):
                load_study_spec(path)

            document["strategies"][0]["search_space"] = {
                "Value": {
                    "kind": "real",
                    "low": 0.0,
                    "high": float(1 << 53),
                    "step": 1.0,
                }
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                StudySpecError, "stepped-real cardinality must not exceed 2\\^53"
            ):
                load_study_spec(path)

    def test_candidate_policy_rejects_unknown_value_and_wrong_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for policy in ("typo", True):
                document = valid_document()
                document["sampler"]["candidate_policy"] = policy
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(policy=policy):
                    with self.assertRaisesRegex(
                        StudySpecError,
                        "must be sampler_default, without_replacement, or exhaustive",
                    ):
                        load_study_spec(path)

    def test_log_dimensions_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = valid_document()
            search_space = document["strategies"][0]["search_space"]
            search_space["Length"] = {
                "kind": "integer",
                "low": 1,
                "high": 1_000_000,
                "step": 1,
                "log": True,
            }
            search_space["Multiplier"] = {
                "kind": "real",
                "low": 1e-6,
                "high": 1e3,
                "log": True,
            }
            path = Path(temporary) / "study.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            spec = load_study_spec(path)

            self.assertTrue(spec.search_space["Length"].log)
            self.assertEqual(spec.search_space["Length"].step, 1)
            self.assertTrue(spec.search_space["Multiplier"].log)
            self.assertIsNone(spec.search_space["Multiplier"].step)

    def test_log_dimensions_reject_invalid_bounds_and_steps(self) -> None:
        invalid = (
            (
                {"kind": "integer", "low": 0, "high": 10, "log": True},
                "low: must be positive for log sampling",
            ),
            (
                {"kind": "integer", "low": -10, "high": 0, "log": True},
                "high: must be positive for log sampling",
            ),
            (
                {
                    "kind": "integer",
                    "low": 1,
                    "high": 10,
                    "step": 2,
                    "log": True,
                },
                "step: must equal 1 for log integer sampling",
            ),
            (
                {"kind": "real", "low": 0.0, "high": 10.0, "log": True},
                "low: must be positive for log sampling",
            ),
            (
                {"kind": "real", "low": -10.0, "high": 0.0, "log": True},
                "high: must be positive for log sampling",
            ),
            (
                {
                    "kind": "real",
                    "low": 1e-3,
                    "high": 1e3,
                    "step": 0.1,
                    "log": True,
                },
                "step: is not supported for log real sampling",
            ),
            (
                {"kind": "boolean", "log": True},
                "log: is only supported for integer and real dimensions",
            ),
            (
                {"kind": "categorical", "choices": ["a", "b"], "log": False},
                "log: is only supported for integer and real dimensions",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for parameter, expected in invalid:
                document = valid_document()
                document["strategies"][0]["search_space"]["Length"] = parameter
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.subTest(parameter=parameter):
                    with self.assertRaisesRegex(StudySpecError, expected):
                        load_study_spec(path)

    def test_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaises(StudySpecError) as caught:
                load_study_spec(path)
            self.assertIn("duplicate JSON key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
