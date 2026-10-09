"""Python route for the return-statistics metrics: names are ordinary metric identifiers inside
objective and constraint expressions; Python forwards them verbatim and relays the native result,
including its result-level `return_stats` object, without reading or recomputing anything.
UNEXECUTED until the spot phase."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

from pineforge_hpo.artifact import StrategyArtifact  # noqa: E402
from pineforge_hpo.cli import _native_command, main  # noqa: E402
from pineforge_hpo.study_spec import load_study_spec  # noqa: E402

CONSTRAINTS = ["returns.bar.count >= 1", "returns.monthly.status != 7"]


class ReturnStatsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        header = self.root / "include" / "pineforge" / "pineforge.h"
        header.parent.mkdir(parents=True)
        header.write_text("/* test engine */\n", encoding="utf-8")
        (self.root / "strategy.pine").write_text("//@version=6\nstrategy('t')\n", encoding="utf-8")
        (self.root / "bars.csv").write_text(
            "timestamp,open,high,low,close,volume\n1,1,1,1,1,1\n", encoding="utf-8")
        self.native = self.root / "pineforge-hpo-native"
        self.native.write_text("", encoding="utf-8")
        self.native.chmod(0o755)
        self.study_path = self.root / "study.json"
        self.study_path.write_text(json.dumps({
            "schema_version": 1, "mode": "single_strategy",
            "strategies": [{
                "id": "test", "source": "strategy.pine", "datasets": ["bars"],
                "fixed_inputs": {}, "strategy_overrides": {},
                "search_space": {"Length": {"kind": "integer", "low": 2, "high": 4, "step": 1}},
            }],
            "datasets": [{"id": "bars", "ohlcv": "bars.csv", "input_tf": "60", "script_tf": "60",
                          "chart_timezone": "UTC"}],
            "objective": {"kind": "expression", "direction": "maximize",
                          "expression": "metrics.all.net_profit", "constraints": CONSTRAINTS},
            "sampler": {"kind": "grid", "seed": 1, "trials": 3},
            "execution": {"workers": 2, "isolation": "threads"},
        }), encoding="utf-8")

    def artifact(self) -> StrategyArtifact:
        return StrategyArtifact(
            artifact_key="a" * 64, request_key="b" * 64,
            plugin_path=self.root / "strategy.dylib",
            generated_cpp_path=self.root / "strategy.cpp",
            manifest_path=self.root / "manifest.json",
            provenance_path=self.root / "provenance.json", cache_hit=False,
            inputs=({"title": "Length", "type": "int"},), strategy_params={})

    def test_statistics_names_are_forwarded_verbatim_and_never_invented(self) -> None:
        command = _native_command(load_study_spec(self.study_path), native=self.native,
                                  plugin=self.root / "strategy.dylib", artifact_key="a" * 64)
        forwarded = [command[index + 1] for index, item in enumerate(command)
                     if item == "--constraint"]
        self.assertEqual(forwarded, CONSTRAINTS)
        # Python adds no request of its own: absent names mean no reduction.
        self.assertNotIn("--record-metric", command)
        study = json.loads(self.study_path.read_text())
        study["objective"]["constraints"] = []
        self.study_path.write_text(json.dumps(study), encoding="utf-8")
        plain = _native_command(load_study_spec(self.study_path), native=self.native,
                                plugin=self.root / "strategy.dylib", artifact_key="a" * 64)
        self.assertNotIn("--constraint", plain)
        self.assertFalse([item for item in plain if str(item).startswith("returns.")])

    @mock.patch("pineforge_hpo.cli.subprocess.run")
    @mock.patch("pineforge_hpo.cli.ArtifactBuilder")
    def test_the_native_return_stats_object_is_relayed_unchanged(self, builder: mock.Mock,
                                                                  run: mock.Mock) -> None:
        builder.return_value.build.return_value = self.artifact()
        stats = {"contract": "pineforge-hpo-return-stats/v1", "series": ["bar", "monthly"],
                 "chart_timezone": "UTC", "risk_free_annual": 0.02,
                 "numeric_build_identity": "pineforge-hpo-return-stats-build/v1:sha256:" + "0" * 64}
        native_result = {"ok": True, "trials": [], "best_trial_id": 0, "best_value": 1.5,
                         "return_stats": stats}
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(native_result), stderr="")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["run", str(self.study_path), "--engine-root", str(self.root),
                         "--native", str(self.native)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["return_stats"], stats)


if __name__ == "__main__":
    unittest.main()
