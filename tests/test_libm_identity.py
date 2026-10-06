"""Differential coverage for the search-space-scoped runtime libm identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    native, plugin, shim = (str(Path(argument).resolve()) for argument in sys.argv[1:])
    functions = [
        "log",
        "log1p",
        "exp",
        "expm1",
        "sqrt",
        "cos",
        "erfc",
        "floor",
        "ceil",
        "round",
        "logl",
        "log1pl",
        "expl",
        "expm1l",
        "sqrtl",
        "cosl",
        "erfcl",
        "floorl",
        "ceill",
        "roundl",
        "fma",
        "fmal",
    ]
    inlined_functions = {
        function + suffix
        for function in ("sqrt", "floor", "ceil", "round", "trunc", "fma")
        for suffix in ("", "f", "l")
    }
    spaces = {
        "linear-real": ["--real-dim", "X", "0", "1", "continuous"],
        "stepped-real": ["--real-dim", "X", "0", "1", "0.01"],
        "linear-int": ["--int-dim", "Length", "1", "99", "1"],
        "log-real": ["--log-real-dim", "X", "0.1", "99"],
        "log-int": ["--log-int-dim", "Length", "1", "99"],
        "categorical": [
            "--categorical-choice",
            "Choice",
            "a",
            "--categorical-choice",
            "Choice",
            "b",
            "--categorical-choice",
            "Choice",
            "c",
        ],
        "bool": ["--bool-dim", "Flag"],
        "categorical+linear-int": [
            "--categorical-choice",
            "Choice",
            "a",
            "--categorical-choice",
            "Choice",
            "b",
            "--int-dim",
            "Length",
            "1",
            "99",
            "1",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="pfh-libm-") as temporary:
        directory = Path(temporary)
        chart = directory / "chart.csv"
        chart.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1700000000000,100,102,99,101,10\n"
            "1700000060000,101,103,100,102,11\n"
        )

        def run(
            dimensions,
            function=None,
            *,
            preload=True,
            ulp=False,
            parent=None,
            trials=None,
        ):
            environment = dict(os.environ)
            for name in ("LD_PRELOAD", "PFH_SHIM", "PFH_SHIM_ULP"):
                environment.pop(name, None)
            if preload:
                environment["LD_PRELOAD"] = shim
            if function:
                environment["PFH_SHIM"] = function
            if ulp:
                environment["PFH_SHIM_ULP"] = "1"
            command = [
                native,
                "run",
                "--strategy",
                plugin,
                "--ohlcv",
                str(chart),
                "--objective",
                "metrics.all.net_profit",
                "--input-tf",
                "1",
                "--script-tf",
                "5",
                "--bar-magnifier",
                "true",
                "--magnifier-samples",
                "6",
                "--magnifier-distribution",
                "triangle",
                "--sampler",
                "tpe",
                "--max-trials",
                str(trials if trials is not None else (5 if parent else 30)),
                "--seed",
                "7",
                "--tpe-startup-trials",
                "6",
                "--workers",
                "1",
                "--batch-size",
                "1",
                *dimensions,
            ]
            if parent:
                command += ["--warm-start", str(parent)]
            process = subprocess.run(
                command, capture_output=True, text=True, env=environment, timeout=30
            )
            require(
                process.returncode == 0, f"{dimensions}/{function}: {process.stderr}"
            )
            return json.loads(process.stdout)

        def proposals(result):
            return [trial["parameters"] for trial in result["trials"]]

        changed_count = 0
        for name, dimensions in spaces.items():
            baseline = run(dimensions, preload=False)
            inactive = run(dimensions)
            identity = baseline["numeric_build_identity"]
            require(
                inactive["numeric_build_identity"] == identity,
                f"{name}: inactive identity differs",
            )
            require(
                proposals(inactive) == proposals(baseline),
                f"{name}: inactive proposals differ",
            )
            mask = int(identity.split(";libm_functions:")[1].split(";")[0])
            changed_functions = []
            unchanged_probe_functions = []
            for index, function in enumerate(functions):
                perturbed = run(dimensions, function)
                identity_changed = perturbed["numeric_build_identity"] != identity
                if identity_changed:
                    require(
                        mask & (1 << index),
                        f"{name}/{function}: excluded function changed identity",
                    )
                elif mask & (1 << index):
                    require(
                        function in inlined_functions,
                        f"{name}/{function}: included function perturbation left identity "
                        "unchanged and is not an allowed compiler intrinsic",
                    )
                    unchanged_probe_functions.append(function)
                if proposals(perturbed) != proposals(baseline):
                    require(
                        identity_changed,
                        f"{name}/{function}: proposals changed without identity",
                    )
                    changed_functions.append(function)
                    changed_count += 1
            print(
                f"PASS libm differential {name}: 22 functions; "
                f"proposal changes={changed_functions}; "
                f"unchanged included probes={unchanged_probe_functions}",
                flush=True,
            )
            parent = directory / f"{name}.json"
            warm_baseline = (
                run(dimensions, preload=False, trials=1)
                if name in {"categorical", "bool"}
                else baseline
            )
            parent.write_text(json.dumps(warm_baseline))
            child = run(dimensions, "log1pl", ulp=True, parent=parent)
            require(
                child["numeric_build_identity"] == identity,
                f"{name}: portable identity depends on log1pl",
            )
            require(
                child["parent_numeric_build_identity"] == identity,
                f"{name}: parent identity lost",
            )
            expected = "restored_sampler_state"
            require(
                child["warm_start_model"] == expected,
                f"{name}: expected {expected}, got {child['warm_start_model']}",
            )
        require(changed_count == 0, "host libm changed portable proposals")
        print(
            "PASS portable math: 176 perturbations, no identity/proposal changes; all warm restores",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
