// Reuse the ABI fixture, with an execution trace and a different curve on every invocation.
// A proof dump reconstructed by rerunning the strategy must fail the invocation-count test.
#include <atomic>
#include <cstdint>
#include <fstream>
#include <mutex>

#define PINEFORGE_HPO_FAKE_PLUGIN 1
#define run_backtest_full pbo_proof_base_run_backtest_full
#include "test_engine_adapter.cpp"
#undef run_backtest_full

namespace {
std::atomic<std::uint64_t> proof_invocations{0};
std::mutex proof_trace_mutex;
}

extern "C" PF_API void run_backtest_full(
    pf_strategy_t strategy, pf_bar_t* bars, int bar_count, const char* input_tf,
    const char* script_tf, int magnifier, int samples,
    pf_magnifier_distribution_t distribution, pf_report_t* report) {
    const auto invocation = ++proof_invocations;
    pbo_proof_base_run_backtest_full(
        strategy, bars, bar_count, input_tf, script_tf, magnifier, samples, distribution, report);
    auto* state = fake(strategy);
    for (std::int64_t index = 0; index < report->equity_curve_len; ++index)
        report->equity_curve[index].equity *= static_cast<double>(invocation);
    const auto trace = state->inputs.find("ProofRunTrace");
    if (trace == state->inputs.end()) {
        state->error = "proof invocation trace was not requested";
        return;
    }
    std::lock_guard<std::mutex> lock(proof_trace_mutex);
    std::ofstream output(trace->second, std::ios::binary | std::ios::app);
    output << state->inputs.at("Length") << ',' << bar_count << ',' << invocation << '\n';
    output.flush();
    if (!output)
        state->error = "cannot write proof invocation trace";
}
