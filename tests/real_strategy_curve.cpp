// Probe for test_return_stats_e2e.py: runs one real compiled strategy through the C ABI and prints
// the engine-owned canonical equity curve and the engine's own Sharpe ratios, exactly.
//
//   real_strategy_curve PLUGIN OHLCV INPUT_TF SCRIPT_TF [NAME=VALUE ...]
//
// Doubles are printed as C99 hexadecimal floats (exact, parsed by float.fromhex), times as
// integers. UNEXECUTED until the spot phase; it uses only the adapter's plugin and dataset
// loaders, like real_strategy_trades.cpp.

#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>

#include <cstdio>
#include <iostream>
#include <stdexcept>
#include <string>

namespace pfh = pineforge::hpo;

namespace {

class Trial final {
public:
    explicit Trial(const pfh::StrategyPlugin& plugin)
        : plugin_(plugin), strategy(plugin.create_strategy()) {}
    ~Trial() {
        plugin_.free_report(&report);
        plugin_.free_strategy(strategy);
    }
    Trial(const Trial&) = delete;
    Trial& operator=(const Trial&) = delete;

private:
    const pfh::StrategyPlugin& plugin_;

public:
    pf_strategy_t strategy;
    pf_report_t report{};
};

std::string hex(double value) {
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "%a", value);
    return buffer;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc < 5)
            throw std::invalid_argument(
                "real_strategy_curve PLUGIN OHLCV INPUT_TF SCRIPT_TF [NAME=VALUE ...]");
        const pfh::StrategyPlugin plugin(argv[1]);
        const auto dataset = pfh::Dataset::load_csv(argv[2]);
        Trial trial(plugin);
        for (int index = 5; index < argc; ++index) {
            const std::string setting = argv[index];
            const auto equals = setting.find('=');
            if (equals == std::string::npos)
                throw std::invalid_argument("input must be NAME=VALUE: " + setting);
            plugin.set_input(trial.strategy, setting.substr(0, equals), setting.substr(equals + 1));
        }
        plugin.run_backtest_full(trial.strategy, dataset.data(), static_cast<int>(dataset.size()),
                                 argv[3], argv[4], false, 4, PF_MAGNIFIER_ENDPOINTS,
                                 &trial.report);
        const auto error = plugin.last_error(trial.strategy);
        if (!error.empty())
            throw std::runtime_error(error);
        const auto& report = trial.report;
        std::cout << "{\"points\":" << report.equity_curve_len
                  << ",\"input_bars_processed\":" << report.input_bars_processed
                  << ",\"script_bars_processed\":" << report.script_bars_processed
                  << ",\"total_trades\":" << report.total_trades
                  << ",\"sharpe_bar\":\"" << hex(report.metrics.equity.sharpe_bar) << '"'
                  << ",\"sharpe_monthly\":\"" << hex(report.metrics.equity.sharpe_monthly) << '"'
                  << ",\"curve\":[";
        for (std::int64_t index = 0; index < report.equity_curve_len; ++index) {
            if (index != 0)
                std::cout << ',';
            std::cout << '[' << report.equity_curve[index].time_ms << ",\""
                      << hex(report.equity_curve[index].equity) << "\"]";
        }
        std::cout << "]}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
