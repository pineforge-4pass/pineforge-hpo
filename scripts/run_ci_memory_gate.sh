#!/usr/bin/env bash
# CI/release wiring only. SM-CX owns ci_memory_gate.py and its measured fixtures.
set -euo pipefail

if ! test -f scripts/ci_memory_gate.py; then
    echo '::error::HPO memory gate INCOMPLETE: scripts/ci_memory_gate.py is missing (SM-CX).'
    exit 2
fi

mkdir -p build
log=build/memory-gate.log
gate_status=0
python3 -u scripts/ci_memory_gate.py 2>&1 | tee "$log" || gate_status=$?
if (( gate_status != 0 )); then
    echo "::error::HPO memory gate failed (exit $gate_status); inspect the per-rung figures above."
    exit "$gate_status"
fi

# Do not accept an exit-0 script which judged nothing, or omitted its count.
# The measurement contract permits either rows or cases in the summary.
python3 - "$log" <<'PY'
from pathlib import Path
import re
import sys

output = Path(sys.argv[1]).read_text()
patterns = (
    r"(?<![\w.+-])([+-]?[0-9]+)\s+(?:rows?|cases?)\b",
    r"\b(?:rows?|cases?)(?:[ _]+judged)?\s*[:=]\s*([+-]?[0-9]+)(?![\w.])",
)
counts = [int(value) for pattern in patterns for value in re.findall(pattern, output, re.I)]
if not counts or any(count <= 0 for count in counts):
    print("::error::HPO memory gate INCOMPLETE: zero rows/cases or no positive judgment count.")
    raise SystemExit(2)
print("HPO memory gate: nonempty judgment confirmed.")
PY
