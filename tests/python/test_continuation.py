from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pineforge_hpo.cli import main, prepare_run
from pineforge_hpo.continuation import (
    SPACE_HASH_VERSION,
    WarmStartError,
    load_warm_start,
    recorded_space,
    space_hash,
    space_info,
)
from pineforge_hpo.study_spec import load_study_spec


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spec = self.root / "study.json"
        self.document = json.loads(
            (
                Path(__file__).parents[2] / "examples/single_strategy/study.json"
            ).read_text()
        )
        self.document["strategies"][0]["source"] = "does-not-exist.pine"
        self.document["datasets"][0]["ohlcv"] = "does-not-exist.csv"
        self.spec.write_text(json.dumps(self.document))
        self.study = load_study_spec(self.spec)
        self.parent = self.root / "parent.jsonl"
        self.trials = [
            {
                "trial_id": identifier,
                "status": "ok",
                "feasible": True,
                "objective": float(identifier),
                "parameters": {"Entry Level": 101 + identifier, "Exit Level": 98},
                "space": recorded_space(self.study),
                "space_hash": space_hash(self.study),
                "space_hash_version": SPACE_HASH_VERSION,
            }
            for identifier in range(2)
        ]
        self.write_jsonl()

    def tearDown(self):
        self.temporary.cleanup()

    def write_jsonl(self):
        self.parent.write_text(
            "".join(json.dumps(trial) + "\n" for trial in self.trials)
        )

    def test_golden_hash_and_read_only_cardinality(self):
        self.assertEqual(
            space_hash(self.study),
            "1188c07588652f2b365a1fc233815d5c51d350e92106051309d9e84ad586071a",
        )
        self.assertEqual(space_info(self.study)["cardinality"], 9)
        info = space_info(self.study, self.parent)
        self.assertEqual((info["tried"], info["remaining"]), (2, 7))
        self.assertFalse((self.root / "does-not-exist.csv").exists())

    def test_checkpoint_checksum_preflight(self):
        payload = "valid opaque payload"
        checkpoint = (
            "PFHTPE2\n" + hashlib.sha256(payload.encode()).hexdigest() + "\n" + payload
        )
        parent = {
            "space": recorded_space(self.study),
            "trials": self.trials,
            "tpe_sampler_state": checkpoint,
        }
        self.parent.write_text(json.dumps(parent))
        self.assertEqual(space_info(self.study, self.parent)["tried"], 2)
        for invalid in (checkpoint + "corrupt", "", 7):
            parent["tpe_sampler_state"] = invalid
            self.parent.write_text(json.dumps(parent))
            with self.assertRaises(WarmStartError):
                load_warm_start(self.study, self.parent)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder,
            ):
                self.assertEqual(
                    main(["run", str(self.spec), "--warm-start", str(self.parent)]), 4
                )
                builder.assert_not_called()

    def test_mixed_typed_unicode_hash_golden(self):
        self.document["strategies"][0]["search_space"] = {
            "count": {"kind": "integer", "low": -2, "high": 4, "step": 2},
            "enabled": {"kind": "boolean"},
            "choice": {"kind": "categorical", "choices": [3, 4.0, True, "臺北\n"]},
            "scale": {"kind": "real", "low": 0.1, "high": 10.0, "log": True},
        }
        self.document["objective"].update(
            expression="metrics.all.net_profit",
            direction="minimize",
            constraints=["2 > 1", "1 > 0"],
        )
        self.document["sampler"].update(kind="tpe", trials=5)
        self.spec.write_text(json.dumps(self.document))
        self.assertEqual(
            space_hash(load_study_spec(self.spec)),
            "fd04b34677f03f8d4d2f49cccb1c58ca42ea9fcc448bbcd05ab72d052c5ecb90",
        )

    def test_formats_sha_and_non_contiguous_ids(self):
        self.trials[1]["trial_id"] = 17
        self.write_jsonl()
        history = load_warm_start(self.study, self.parent)
        self.assertEqual(history.next_id, 18)
        self.assertEqual(
            history.source_sha256, hashlib.sha256(self.parent.read_bytes()).hexdigest()
        )
        for document in (
            self.trials,
            {"trials": self.trials, "space": recorded_space(self.study)},
        ):
            self.parent.write_text(json.dumps(document))
            self.assertEqual(space_info(self.study, self.parent)["tried"], 2)

    def test_repeated_points_count_once_but_ids_remain_distinct(self):
        self.trials[1]["parameters"] = self.trials[0]["parameters"]
        self.write_jsonl()
        history = load_warm_start(self.study, self.parent)
        self.assertEqual(len(history.trials), 2)
        self.assertEqual(len(history.tried), 1)

    def test_all_terminal_statuses_follow_live_observation_rules(self):
        for status in (
            "constraint_violation",
            "engine_error",
            "objective_error",
            "constraint_error",
            "trial_error",
            "trial_timeout",
            "pruned",
            "partial",
        ):
            with self.subTest(status=status):
                self.trials[1].update(status=status, feasible=False, objective=None)
                self.trials[1]["pruning"] = {"rung_scores": [1.0, None]}
                self.write_jsonl()
                history = load_warm_start(self.study, self.parent)
                self.assertEqual(history.feasible, 1)
                self.assertEqual(
                    history.completed, 2 if status == "constraint_violation" else 1
                )

    def test_refuses_unknown_status_and_duplicate_ids(self):
        for change in ({"status": "surprise"}, {"trial_id": 0}, {"trial_id": 1.0}):
            with self.subTest(change=change):
                trials = copy.deepcopy(self.trials)
                trials[1].update(change)
                self.parent.write_text(json.dumps(trials))
                with self.assertRaises(WarmStartError):
                    load_warm_start(self.study, self.parent)

    def test_refuses_every_compatibility_axis_before_build(self):
        changes = (
            lambda space: space["parameters"]["Entry Level"].update(high=104),
            lambda space: space["parameters"]["Entry Level"].update(low=100),
            lambda space: space["parameters"]["Entry Level"].update(step=0.5),
            lambda space: space["parameters"]["Entry Level"].update(log=True),
            lambda space: space["parameters"]["Entry Level"].update(kind="integer"),
            lambda space: space["parameters"].pop("Exit Level"),
            lambda space: space["objective"].update(direction="minimize"),
            lambda space: space["objective"].update(
                expression="metrics.all.num_trades"
            ),
            lambda space: space["objective"].update(constraints=[]),
        )
        for change in changes:
            trials = copy.deepcopy(self.trials)
            change(trials[0]["space"])
            self.parent.write_text(json.dumps(trials))
            with mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder:
                with self.assertRaises(WarmStartError):
                    prepare_run(self.spec, warm_start=self.parent)
                builder.assert_not_called()

    def test_trial_id_capacity_is_checked_before_build(self):
        for offset in (2, 3):
            self.trials[1]["trial_id"] = (1 << 64) - offset
            self.write_jsonl()
            with mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder:
                with self.assertRaisesRegex(WarmStartError, "overflow trial IDs"):
                    prepare_run(self.spec, warm_start=self.parent)
                builder.assert_not_called()

    def test_old_hash_version_recomputed_from_recorded_space(self):
        for trial in self.trials:
            trial.update(space_hash_version=0, space_hash="old-version-digest")
        self.write_jsonl()
        self.assertEqual(
            space_info(self.study, self.parent)["space_hash"], space_hash(self.study)
        )

    def test_version_and_pin_metadata_do_not_affect_identity(self):
        baseline = space_hash(self.study)
        self.parent.write_text(
            json.dumps(
                {
                    "pineforge_hpo_version": "99.0",
                    "pin": "different-engine-codegen",
                    "space": recorded_space(self.study),
                    "trials": self.trials,
                }
            )
        )
        self.assertEqual(space_info(self.study, self.parent)["space_hash"], baseline)

    def test_null_propagated_objective_and_legacy_embedded_spec(self):
        self.trials[0]["objective"] = None
        records = [
            {key: value for key, value in trial.items() if key != "space"}
            for trial in self.trials
        ]
        self.parent.write_text(
            json.dumps({"study_spec": self.document, "trials": records})
        )
        history = load_warm_start(self.study, self.parent)
        self.assertEqual((history.completed, history.feasible), (2, 2))

    def test_current_hash_and_complete_ancestry_are_checked(self):
        for document in (
            {
                "space": recorded_space(self.study),
                "trials": self.trials,
                "space_hash_version": 1,
                "space_hash": "incorrect",
            },
            {
                "space": recorded_space(self.study),
                "trials": self.trials,
                "trials_completed": 3,
            },
            {
                "space": recorded_space(self.study),
                "trials": self.trials,
                "warm_start": {"trials": 2},
            },
        ):
            self.parent.write_text(json.dumps(document))
            with self.assertRaises(WarmStartError):
                load_warm_start(self.study, self.parent)

    def test_exhaustion_refuses_before_build_and_info_still_works(self):
        space = self.document["strategies"][0]["search_space"]
        space["Entry Level"].update(low=101, high=102)
        space["Exit Level"].update(low=98, high=98)
        self.spec.write_text(json.dumps(self.document))
        self.study = load_study_spec(self.spec)
        for trial in self.trials:
            trial["space"] = recorded_space(self.study)
            trial["space_hash"] = space_hash(self.study)
        self.write_jsonl()
        self.assertEqual(space_info(self.study, self.parent)["remaining"], 0)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder,
        ):
            self.assertEqual(
                main(["run", str(self.spec), "--warm-start", str(self.parent)]), 5
            )
            builder.assert_not_called()

    def test_partial_result_and_missing_space_fail_closed(self):
        for document in (
            {"trials": self.trials, "trials_out": "summary"},
            [
                {key: value for key, value in trial.items() if key != "space"}
                for trial in self.trials
            ],
        ):
            self.parent.write_text(json.dumps(document))
            with self.assertRaises(WarmStartError):
                load_warm_start(self.study, self.parent)

    def test_read_only_cli_and_exit_codes(self):
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder,
        ):
            self.assertEqual(
                main(
                    [
                        "space-info",
                        "--spec",
                        str(self.spec),
                        "--warm-start",
                        str(self.parent),
                    ]
                ),
                0,
            )
            builder.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["remaining"], 7)
        self.trials[0]["status"] = "unknown"
        self.write_jsonl()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["run", str(self.spec), "--warm-start", str(self.parent)]), 4
            )

    def test_finite_warm_budget_refuses_before_build(self):
        for policy in ("without_replacement", "exhaustive"):
            self.document["sampler"].update(candidate_policy=policy, trials=8)
            self.spec.write_text(json.dumps(self.document))
            with (
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch("pineforge_hpo.cli.ArtifactBuilder") as builder,
            ):
                self.assertEqual(
                    main(["run", str(self.spec), "--warm-start", str(self.parent)]), 4
                )
                builder.assert_not_called()

    def test_continuous_info_has_null_cardinality_and_remaining(self):
        self.document["sampler"].update(kind="random", trials=2)
        self.document["strategies"][0]["search_space"]["Entry Level"].pop("step")
        self.spec.write_text(json.dumps(self.document))
        info = space_info(load_study_spec(self.spec))
        self.assertIsNone(info["cardinality"])
        self.assertIsNone(info["remaining"])


if __name__ == "__main__":
    unittest.main()
