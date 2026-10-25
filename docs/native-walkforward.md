## Selected-window plugin binding and OOS evaluation

`StrategyPlugin` optionally binds the engine's six selected-window and execution-observer
exports. Ordinary execution requires none of them. Selected execution refuses the first missing
export in protocol order and lists all missing exports in its diagnostic, then checks both wire
versions are 1. It next reads `pf_version_get` and `pf_version_string` from the loaded plugin:
numeric components must agree, the runtime must be stable engine 1.x at least 1.6.2, and any
supplied expected manifest version must exactly match the full embedded version. Release
candidates are refused, including dirty release candidates. Git-describe versions use
`X.Y.Z[-rc.N][-D-gSHA][-dirty]`: dirty trees are admitted with or without a distance/SHA suffix,
including an exactly tagged dirty tree such as `1.7.0-dirty`. The exact full string is the
attribution recorded in the manifest and compared by `engine_identity`. This release gate does
not attest a custom build's provenance or replace real-plugin parity evidence.

The six exports, in admission order, are `pf_selected_window_version`,
`strategy_set_selected_window_v1`, `strategy_selected_window_counts_v1`,
`pf_execution_observer_version`, `strategy_set_execution_observer_v1`, and
`strategy_execution_observation_v1`. Missing exports, incompatible wire versions, contract
refusals and engine-identity refusals use `hpo_walkforward_unavailable` with distinct reasons.
Setter allocation refusal (-4) is engine `out_of_memory`. Other setter refusals, getter
refusals, mutated headers and post-run correlation failures use `hpo_invariant`; a setter's -2
uses `selected_window_contract` or `execution_observer_contract`. Counts -2 after a successful
run never authorizes scoring.

`SelectedEvaluation::evaluate` consumes already-planned `[T,E)` bounds, an immutable retained
row pointer/count, and six expected counts. It neither loads nor calls the planner. The expected
`preroll_script_bars` is the planner's **used** script groups. Each call admits the plugin,
creates one fresh handle and applies the same timezone, overrides, inputs, symbol metadata and
feed setter sequence as an ordinary trial. It optionally reads version-1 effective-settings JSON
from that handle using BUFFER_TOO_SMALL then OK, with a 16 MiB bound. An absent or unusable
receipt yields `effective_settings == nullopt`; consumers must treat that as **UNATTESTED**,
never as a settings attestation.

The helper configures selection, installs a native observer and invokes `run_backtest_full`
once on the retained pointer. Fresh handles keep the engine's full owning report mode; this
helper never requests summary-only mode, which the observer refuses. The callback catches all
exceptions, records exactly one boundary and replaces the receipt's UINT32_MAX sentinel with
the valid non-exporting 0/0/0/0 receipt. After return, a retained callback failure wins over engine
errors. The helper then checks the engine error/code/status, the one successful callback,
three nonzero count/observation identities, six plan/count equalities, Results/ResultsOpen state,
boundary delivery and absence of faults, callback identity, processed **fed** counts and
`window_script_bars + 1` equity points. Only then does shared report capture create a snapshot.
The objective layer can score only a returned snapshot.

The engine harness also checks `report.input_tf_seconds` against the request; this helper has no
equivalent check. Timeframe and feed admission belong to the planner lanes (4a/4b).

When `pbo_blocks` is requested, the block layout and moments depend on the selected window's
curve length. Blocks are comparable only across trials of the **same window**.

Cleanup removes the observer while its context is alive, clears pending selection, then frees
the report before the handle. If removal fails, the context survives handle destruction.
Cleanup cannot replace an earlier failure, and a cleanup refusal cannot return a successful
result. Returned values include the native counts, observation, boundary and optional settings
receipt alongside the report.

At most **one selected evaluation may be in flight per worker**. The helper copies the report,
including trades and equity even if ordinary capture flags are disabled, and immediately releases
the engine report. The detached report costs O(window bars + trades); the caller must stream its
window row, trade rows and equity segment to its sink and release the value before evaluating the
next window. The helper retains no window history.

Unit fixtures are explicitly opted in and separately named. The default fake has none of the six
exports, enforced by source inspection and post-build/CTest defined-symbol guards on Linux and
Darwin. These tests do not establish real-engine selected behavior. Real current-plugin parity,
an older missing-export archive and a parity-era revision refusal require the separate real-plugin
gate. The adapter and these native tests require the full dispatched CI build on Ubuntu and
macOS or a box; PR core CI does not compile this path.
