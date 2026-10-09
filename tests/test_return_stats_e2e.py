"""Real compiled-Pine proofs of the return-statistics metrics (legacy mode, contract
`pineforge-hpo-return-stats/v1`). Run by hand on a spot box, like test_warm_start_e2e.py:

    python3 tests/test_return_stats_e2e.py --native build/bin/pineforge-hpo-native \\
        --output <dir> [--pinned-reference BINARY --pinned-sha256 HEX] [--skip-timing]
        [--timing-case 8760|2000000] [--only-timing] [--repeats N]

The 2,000,000-bar case is the long one: run it as its own invocation (--timing-case 2000000
--only-timing) so a heavy proof can be sharded and harvested separately.

UNEXECUTED until the spot phase. What it proves, and what it does not:

1. Reconciliation on a real canonical curve (8,760 hourly script bars, about one UTC year). The
   probe `tests/real_strategy_curve.cpp` prints the engine-owned curve and the engine's own
   `sharpe_bar` / `sharpe_monthly` exactly (hexadecimal floats). Three independent references are
   compared with the native row values for both series:
   (a) exact rational arithmetic with 60-digit Decimal roots (bound: REL_TOL, a bound for binary64
       evaluation of the pinned operation order, not a claim of accuracy);
   (b) a Python binary64 mirror of the pinned operation order, expected bit-identical on this
       host and reported for the tested pair only;
   (c) the engine's own Sharpe ratios. Common valid domain: finite equities, returns only from a
       prior equity above zero, bar series with at least three points and a positive span, at
       least two returns and a positive deviation. Scale: engine value = reducer
       sharpe_per_period * sqrt(periods_per_year), where the bar periods_per_year is
       (n - 1) / span_years and the monthly one is 12, both with risk-free 0.02 / P and the sample
       (N - 1) deviation; compared with REL_TOL_ENGINE because the engine's own summation order is
       not the pinned one.
2. Rows are byte-identical across workers 1, 2, 4, 8 and a repeat, on a real strategy.
3. The Python route (StudySpec) builds the same request.
4. Added time per trial against identical OFF trials at about 8,760 and 2,000,000 script bars, from
   the runner's own scheduler statistics, with realized counts and the measurement scope printed.
   No threshold is asserted: the numbers are the evidence, and a negligible-cost statement needs
   both cases.
5. With --pinned-reference/--pinned-sha256: OFF bytes equal the pinned release binary's.

It is not a cross-architecture claim: every equality is reported for the host that ran it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from decimal import Decimal, getcontext
from fractions import Fraction
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "external" / "pineforge-codegen-oss"))

FIELDS = ("count", "skipped", "periods_per_year", "mean", "std", "sharpe_per_period", "skew",
          "kurt_raw", "status")
NAMES = tuple(f"returns.{series}.{field}" for series in ("bar", "monthly") for field in FIELDS)
RF = 0.02
YEAR_MS = 365.25 * 86400 * 1000
REL_TOL = Decimal("1e-8")         # binary64 pinned order vs exact rationals
REL_TOL_ENGINE = Decimal("1e-8")  # engine summation order vs the pinned one
HOURLY_BARS = 8760
MINUTE_BARS = 2_000_000
START_MS = 1672531200000  # 2023-01-01T00:00:00Z

getcontext().prec = 60


def require(condition, message):
    if not condition:
        raise AssertionError(message)


# ---- independent references ---------------------------------------------------------------


def bar_returns(curve):
    returns, skipped = [], 0
    for (_, prior), (_, current) in zip(curve, curve[1:]):
        if not prior > 0.0:
            skipped += 1
            continue
        returns.append(current / prior - 1.0)  # binary64, the pinned definition
    return returns, skipped


def month_key(time_ms):
    import datetime
    moment = datetime.datetime.fromtimestamp(time_ms // 1000, datetime.timezone.utc)
    return moment.year * 12 + moment.month


def monthly_returns(curve):
    ends, key, last = [], None, None
    for time_ms, equity in curve:
        current = month_key(time_ms)
        if key is not None and current != key:
            ends.append(last)
        key, last = current, equity
    if last is not None:
        ends.append(last)
    returns, skipped = [], 0
    for prior, current in zip(ends, ends[1:]):
        if not prior > 0.0:
            skipped += 1
            continue
        returns.append(current / prior - 1.0)
    return returns, skipped


def mirror(returns, period):
    """Pinned operation order in Python binary64 (no fused multiply-add, no reassociation)."""
    count = len(returns)
    out = dict.fromkeys(FIELDS)
    out["count"] = count
    out["periods_per_year"] = period
    if count < 1:
        return out
    total = 0.0
    for value in returns:
        total += value
    mean = total / count
    out["mean"] = mean
    s2 = s3 = s4 = 0.0
    for value in returns:
        d = value - mean
        s2 += d * d
        s3 += (d * d) * d
        s4 += (d * d) * (d * d)
    if count >= 2:
        out["std"] = math.sqrt(s2 / (count - 1))
        if period is not None and out["std"] > 0.0:
            out["sharpe_per_period"] = (mean - RF / period) / out["std"]
    if count >= 3 and s2 > 0.0:
        out["skew"] = (s3 / count) / ((s2 / count) * math.sqrt(s2 / count))
    if count >= 4 and s2 > 0.0:
        out["kurt_raw"] = (s4 / count) / ((s2 / count) * (s2 / count))
    return out


def exact(returns, period):
    """The same statistics from exact rationals; roots in 60-digit Decimal."""
    count = len(returns)
    out = dict.fromkeys(("mean", "std", "sharpe_per_period", "skew", "kurt_raw"))
    if count < 1:
        return out
    rationals = [Fraction(value) for value in returns]
    mean = sum(rationals) / count
    centred = [value - mean for value in rationals]
    s2 = sum(d * d for d in centred)
    s3 = sum(d * d * d for d in centred)
    s4 = sum(d * d * d * d for d in centred)

    def dec(fraction):
        return Decimal(fraction.numerator) / Decimal(fraction.denominator)

    out["mean"] = dec(mean)
    if count >= 2 and s2 > 0:
        std = (dec(s2) / (count - 1)).sqrt()
        out["std"] = std
        if period is not None:
            out["sharpe_per_period"] = (dec(mean) - Decimal(RF) / Decimal(period)) / std
    if count >= 3 and s2 > 0:
        var = dec(s2) / count
        out["skew"] = (dec(s3) / count) / (var * var.sqrt())
    if count >= 4 and s2 > 0:
        var = dec(s2) / count
        out["kurt_raw"] = (dec(s4) / count) / (var * var)
    return out


def close(native, reference, tolerance):
    if reference is None:
        return native is None
    if native is None:
        return False
    reference = Decimal(reference)
    return abs(Decimal(native) - reference) <= tolerance * max(Decimal(1), abs(reference))


# ---- data and process helpers ----------------------------------------------------------------


def write_bars(path: Path, count: int, step_ms: int) -> None:
    lines = ["timestamp,open,high,low,close,volume"]
    for index in range(count):
        price = 100 + 5 * math.sin(index * 0.15) + 1.5 * math.sin(index * 0.0113)
        lines.append(f"{START_MS + index * step_ms},{price:.6f},{price + 1:.6f},"
                     f"{price - 1:.6f},{price + 0.1:.6f},100")
    path.write_text("\n".join(lines) + "\n")


def run(command, **kwargs):
    process = subprocess.run(command, capture_output=True, timeout=kwargs.pop("timeout", 3600),
                             **kwargs)
    return process


def native_command(native, plugin, csv, tf, *extra):
    # Entry Level 95 to 130 straddles the synthetic price band, so the curve really moves.
    return [str(native), "run", "--strategy", plugin, "--ohlcv", str(csv),
            "--objective", "metrics.all.net_profit", "--input-tf", tf, "--script-tf", tf,
            "--chart-timezone", "UTC", "--real-dim", "Entry Level", "95", "130", "1",
            "--fixed-input", "Exit Level", "98", "--sampler", "grid", *extra]


def record_flags(names=NAMES):
    return [item for name in names for item in ("--record-metric", name)]


def stripped(document):
    document = json.loads(json.dumps(document))
    document.pop("return_stats", None)
    for trial in document["trials"]:
        for name in NAMES:
            trial["metrics"].pop(name, None)
    return document


# ---- the proofs ---------------------------------------------------------------------------------


def reconcile(args, directory, plugin, csv):
    probe = directory / "real-strategy-curve"
    subprocess.run(
        ["g++", "-std=c++17", "-O2", "-ffp-contract=off", "-I", str(ROOT / "include"),
         "-I", str(ROOT / "external/pineforge-engine/include"),
         "-I", str(ROOT / "external/pineforge-engine/build/include"),
         str(ROOT / "tests/real_strategy_curve.cpp"),
         str(ROOT / "src/engine_adapter/strategy_plugin.cpp"),
         str(ROOT / "src/engine_adapter/dataset.cpp"), "-ldl", "-o", str(probe)],
        check=True, timeout=300)
    dumped = run([str(probe), plugin, str(csv), "60", "60", "Entry Level=101", "Exit Level=98"],
                 check=True)
    (directory / "canonical-curve.json").write_bytes(dumped.stdout)
    engine = json.loads(dumped.stdout)
    curve = [(time_ms, float.fromhex(equity)) for time_ms, equity in engine["curve"]]
    require(len(curve) == engine["points"] and len(curve) >= 3, "unusable canonical curve")
    sharpe_bar = float.fromhex(engine["sharpe_bar"])
    sharpe_monthly = float.fromhex(engine["sharpe_monthly"])

    # One grid trial at Entry Level 101: a one-point real dimension, so the row is that vector.
    command = [str(args.native), "run", "--strategy", plugin, "--ohlcv", str(csv),
               "--objective", "metrics.all.net_profit", "--input-tf", "60", "--script-tf", "60",
               "--chart-timezone", "UTC", "--real-dim", "Entry Level", "101", "101", "1",
               "--fixed-input", "Exit Level", "98", "--sampler", "grid", "--max-trials", "1",
               *record_flags(), "--record-metric", "report.input_bars_processed"]
    completed = run(command)
    require(completed.returncode == 0, completed.stderr.decode())
    (directory / "native-reconcile.json").write_bytes(completed.stdout)
    result = json.loads(completed.stdout)
    row = result["trials"][0]["metrics"]

    span_years = (curve[-1][0] - curve[0][0]) / YEAR_MS
    bar_period = (len(curve) - 1) / span_years
    for series, builder, period in (("bar", bar_returns, bar_period),
                                    ("monthly", monthly_returns, 12.0)):
        returns, skipped = builder(curve)
        native = {field: row[f"returns.{series}.{field}"] for field in FIELDS}
        require(native["count"] == len(returns) and native["skipped"] == skipped,
                f"{series}: count/skipped differ from the independent walk")
        require(native["periods_per_year"] == period, f"{series}: periods_per_year differs")
        reference = mirror(returns, period)
        for field in ("mean", "std", "sharpe_per_period", "skew", "kurt_raw"):
            require(native[field] == reference[field],
                    f"{series}.{field}: native {native[field]!r} is not the binary64 mirror "
                    f"{reference[field]!r} on this host")
        high = exact(returns, period)
        for field, value in high.items():
            require(close(native[field], value, REL_TOL),
                    f"{series}.{field}: native {native[field]!r} vs exact {value}")
        every_field_finite = all(native[f] is not None for f in FIELDS[:-1])
        require((native["status"] == 0) == every_field_finite, f"{series}: status iff violated")
        print(f"PASS RECONCILE {series}: T={native['count']} skipped={skipped} "
              f"P={native['periods_per_year']} status={native['status']} "
              "(binary64 mirror bit-identical on this host; exact within REL_TOL)", flush=True)

    # Engine Sharpe ratios on the common valid domain, with the scale named in the docstring.
    for name, engine_value, series, period in (
            ("sharpe_bar", sharpe_bar, "bar", bar_period),
            ("sharpe_monthly", sharpe_monthly, "monthly", 12.0)):
        native = row[f"returns.{series}.sharpe_per_period"]
        if math.isnan(engine_value):
            require(native is None, f"engine {name} is NaN but the reducer has a value")
            continue
        require(native is not None, f"engine {name} exists but the reducer has none")
        scaled = native * math.sqrt(period)
        require(close(scaled, engine_value, REL_TOL_ENGINE),
                f"{name}: engine {engine_value!r} vs reducer*sqrt(P) {scaled!r}")
        print(f"PASS ENGINE {name}: engine={engine_value!r} reducer*sqrt(P)={scaled!r}", flush=True)
    return result


def workers_and_route(args, directory, plugin, csv):
    reference = None
    for workers in (1, 2, 4, 8, 4):
        completed = run(native_command(args.native, plugin, csv, "60", "--max-trials", "16",
                                       "--workers", str(workers), "--batch-size", "4",
                                       *record_flags()))
        require(completed.returncode == 0, completed.stderr.decode())
        document = json.loads(completed.stdout)
        signature = json.dumps({"trials": document["trials"],
                                "return_stats": document["return_stats"]}, sort_keys=True)
        reference = reference or signature
        require(signature == reference, f"rows changed at workers={workers}")
    print("PASS WORKERS: rows and return_stats identical at workers 1/2/4/8 and a repeat",
          flush=True)

    from pineforge_hpo.cli import prepare_run
    spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    spec["strategies"][0].update(
        source=str(directory / "threshold.pine"), fixed_inputs={"Exit Level": 98.0},
        search_space={"Entry Level": {"kind": "real", "low": 1.0, "high": 400.0, "step": 1.0}})
    spec["datasets"][0].update(ohlcv=str(csv), input_tf="60", script_tf="60")
    spec["objective"].update(expression="metrics.all.net_profit",
                             constraints=["returns.bar.count >= 1"])
    spec["sampler"] = {"kind": "grid", "seed": 1, "trials": 4}
    spec["execution"].update(workers=2)
    spec_path = directory / "study-route.json"
    spec_path.write_text(json.dumps(spec))
    command, _ = prepare_run(spec_path, ROOT / "external/pineforge-engine", directory / "cache",
                             native=args.native, compiler="g++",
                             eigen_include="/usr/include/eigen3")
    require("returns.bar.count >= 1" in command, "the route lost the statistics constraint")
    completed = run(command)
    require(completed.returncode == 0, completed.stderr.decode())
    routed = json.loads(completed.stdout)
    require(routed["return_stats"]["series"] == ["bar"], "the route requested the wrong series")
    require(all(t["metrics"]["returns.bar.count"] > 0 for t in routed["trials"]),
            "routed rows lack bar statistics")
    print("PASS ROUTE: the Python route requests exactly the series its expressions name",
          flush=True)


def added_time(args, directory, plugin, label, csv, tf, trials, repeats):
    """Runner busy time per trial, statistics ON against identical OFF trials (ABBA order)."""
    def one(on: bool, index: int):
        stats_file = directory / f"scheduler-{label}-{'on' if on else 'off'}-{index}.json"
        command = native_command(args.native, plugin, csv, tf, "--max-trials", str(trials),
                                 "--workers", "1", "--scheduler-stats", str(stats_file),
                                 "--record-metric", "report.input_bars_processed",
                                 "--record-metric", "report.script_bars_processed",
                                 *(record_flags() if on else []))
        completed = run(command)
        require(completed.returncode == 0, completed.stderr.decode())
        document = json.loads(completed.stdout)
        busy = json.loads(stats_file.read_text())["busy_seconds"]
        rows = document["trials"]
        return busy / len(rows), document, rows[0]["metrics"]

    off, on = [], []
    realized = None
    for index in range(repeats):
        order = (False, True, True, False) if index % 2 == 0 else (True, False, False, True)
        for flag in order:
            per_trial, document, metrics = one(flag, index)
            (on if flag else off).append(per_trial)
            if flag and realized is None:
                realized = (metrics["report.input_bars_processed"],
                            metrics["report.script_bars_processed"],
                            metrics["returns.bar.count"] + metrics["returns.bar.skipped"] + 1)
            if flag:
                require(document["return_stats"]["series"] == ["bar", "monthly"], "wrong series")
    median_off, median_on = statistics.median(off), statistics.median(on)
    print(json.dumps({
        "case": label, "trials_per_run": trials, "runs_each": len(off),
        "realized_input_bars": realized[0], "realized_script_bars": realized[1],
        "realized_curve_points": realized[2],
        "off_seconds_per_trial_median": median_off, "on_seconds_per_trial_median": median_on,
        "added_seconds_per_trial": median_on - median_off,
        "added_percent_of_off": 100.0 * (median_on - median_off) / median_off,
        "off_runs": off, "on_runs": on,
        "scope": "scheduler busy_seconds per completed trial, one worker: engine run, objective, "
                 "recorded metrics and the requested reduction; excludes dataset load, process "
                 "start and result serialization of the whole study",
    }, indent=2), flush=True)


def main():
    from pineforge_hpo.cli import prepare_run

    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pinned-reference", type=Path)
    parser.add_argument("--pinned-sha256")
    parser.add_argument("--skip-timing", action="store_true")
    parser.add_argument("--timing-case", choices=("both", "8760", "2000000"), default="both",
                        help="run one added-time case per invocation to shard a long proof")
    parser.add_argument("--only-timing", action="store_true",
                        help="skip reconciliation, workers, route and pinned comparisons")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    args.native = args.native.resolve()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    hourly = directory / "hourly.csv"
    write_bars(hourly, HOURLY_BARS, 3600 * 1000)
    source = directory / "threshold.pine"
    source.write_text((ROOT / "examples/single_strategy/threshold.pine").read_text()
                      .replace("minval=101, maxval=103", "minval=1, maxval=400"))
    spec = json.loads((ROOT / "examples/single_strategy/study.json").read_text())
    spec["strategies"][0].update(
        source=str(source), fixed_inputs={"Exit Level": 98.0},
        search_space={"Entry Level": {"kind": "real", "low": 1.0, "high": 400.0, "step": 1.0}})
    spec["datasets"][0].update(ohlcv=str(hourly), input_tf="60", script_tf="60")
    spec["objective"].update(expression="metrics.all.net_profit", constraints=[])
    spec["sampler"] = {"kind": "grid", "seed": 1, "trials": 4}
    spec_path = directory / "study.json"
    spec_path.write_text(json.dumps(spec))
    _, artifact = prepare_run(spec_path, ROOT / "external/pineforge-engine", directory / "cache",
                              native=args.native, compiler="g++",
                              eigen_include="/usr/include/eigen3")
    plugin = artifact["plugin"]

    if not args.only_timing:
        reconcile(args, directory, plugin, hourly)
        workers_and_route(args, directory, plugin, hourly)

    if args.pinned_reference is not None and not args.only_timing:
        require(args.pinned_sha256, "--pinned-sha256 is mandatory with --pinned-reference")
        digest = hashlib.sha256(args.pinned_reference.read_bytes()).hexdigest()
        require(digest == args.pinned_sha256.lower(), f"pinned binary digest {digest} differs")
        flags = ("--max-trials", "8", "--workers", "2", "--batch-size", "4")
        old = run(native_command(args.pinned_reference, plugin, hourly, "60", *flags))
        new = run(native_command(args.native, plugin, hourly, "60", *flags))
        require(old.returncode == new.returncode and old.stdout == new.stdout,
                "OFF bytes differ from the pinned release binary")
        on = run(native_command(args.native, plugin, hourly, "60", *flags, *record_flags()))
        require(stripped(json.loads(on.stdout)) == json.loads(old.stdout),
                "a requested run differs from the pinned bytes beyond the additive pieces")
        print("PASS PINNED: OFF bytes equal the pinned binary; requested differs only additively",
              flush=True)

    if not args.skip_timing:
        if args.timing_case in ("both", "8760"):
            added_time(args, directory, plugin, "about-8760-script-bars", hourly, "60", 30,
                       args.repeats)
        if args.timing_case in ("both", "2000000"):
            minute = directory / "minute.csv"
            write_bars(minute, MINUTE_BARS, 60 * 1000)
            added_time(args, directory, plugin, "2000000-script-bars", minute, "1", 3,
                       args.repeats)


if __name__ == "__main__":
    main()
