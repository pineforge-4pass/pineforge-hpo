#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>

#include <chrono>
#include <cstring>
#include <dlfcn.h>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace pfh = pineforge::hpo;

struct Snapshot {
    double objective;
    std::string trades;
};

Snapshot snapshot(const pf_report_t& report, double drawdown_weight) {
    std::ostringstream trades;
    trades << std::hexfloat;
    for (int index = 0; index < report.trades_len; ++index) {
        const auto& trade = report.trades[index];
        trades << trade.entry_time << ':' << trade.exit_time << ':' << trade.entry_price << ':'
               << trade.exit_price << ':' << trade.pnl << ':' << trade.pnl_pct << ':'
               << trade.is_long << ':' << trade.max_runup << ':' << trade.max_drawdown << ':'
               << trade.qty << ':' << trade.commission << ':' << trade.entry_bar_index << ':'
               << trade.exit_bar_index << ':' << trade.open_at_end << ';';
    }
    return {report.metrics.all.net_profit -
                drawdown_weight * report.metrics.equity.max_equity_drawdown, trades.str()};
}

template <typename Function>
Function symbol(void* library, const char* name) {
    void* address = ::dlsym(library, name);
    if (!address)
        throw std::runtime_error(std::string("missing streaming export: ") + name);
    Function result;
    static_assert(sizeof(result) == sizeof(address));
    std::memcpy(&result, &address, sizeof(result));
    return result;
}

int main(int argc, char** argv) {
    if (argc < 6)
        return 1;
    try {
        pfh::StrategyPlugin plugin(argv[1]);
        const auto dataset = pfh::Dataset::load_csv(argv[2]);
        const double drawdown_weight = std::stod(argv[5]);
        const std::vector<std::size_t> counts{
            (dataset.size() + 3) / 4, (dataset.size() + 1) / 2, dataset.size()};
        const auto configure = [&](pf_strategy_t strategy) {
            for (int index = 6; index < argc; ++index) {
                const std::string input(argv[index]);
                const auto separator = input.find('=');
                plugin.set_input(strategy, input.substr(0, separator), input.substr(separator + 1));
            }
        };
        std::vector<Snapshot> reference;
        std::int64_t prefix_bars = 0;
        const auto prefix_start = std::chrono::steady_clock::now();
        for (auto count : counts) {
            auto strategy = plugin.create_strategy();
            configure(strategy);
            pf_report_t report{};
            plugin.run_backtest_full(strategy, dataset.data(), static_cast<int>(count),
                                     argv[3], argv[4], false, 4, PF_MAGNIFIER_ENDPOINTS, &report);
            const auto error = plugin.last_error(strategy);
            if (!error.empty())
                throw std::runtime_error(error);
            reference.push_back(snapshot(report, drawdown_weight));
            prefix_bars += report.input_bars_processed;
            plugin.free_report(&report);
            plugin.free_strategy(strategy);
        }
        const double prefix_seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - prefix_start).count();
        void* library = ::dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
        if (!library)
            throw std::runtime_error("cannot open streaming plugin");
        const auto begin = symbol<int (*)(pf_strategy_t, const pf_bar_t*, int,
                                         const char*, const char*)>(library, "strategy_stream_begin");
        const auto push = symbol<int (*)(pf_strategy_t, const pf_bar_t*)>(
            library, "strategy_stream_push_bar");
        const auto fill = symbol<int (*)(pf_strategy_t, pf_report_t*)>(
            library, "strategy_stream_fill_report");
        auto strategy = plugin.create_strategy();
        configure(strategy);
        const auto stream_start = std::chrono::steady_clock::now();
        if (begin(strategy, dataset.data(), 1, argv[3], argv[4]) != 0)
            throw std::runtime_error(plugin.last_error(strategy));
        std::size_t processed = 1;
        std::vector<bool> trades_equal;
        std::vector<bool> objectives_equal;
        std::vector<double> stream_objectives;
        for (std::size_t rung = 0; rung < counts.size(); ++rung) {
            while (processed < counts[rung]) {
                if (push(strategy, dataset.data() + processed) != 0)
                    throw std::runtime_error(plugin.last_error(strategy));
                ++processed;
            }
            pf_report_t report{};
            if (fill(strategy, &report) != 0)
                throw std::runtime_error(plugin.last_error(strategy));
            const auto current = snapshot(report, drawdown_weight);
            trades_equal.push_back(current.trades == reference[rung].trades);
            objectives_equal.push_back(current.objective == reference[rung].objective);
            stream_objectives.push_back(current.objective);
            plugin.free_report(&report);
        }
        const double stream_seconds = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - stream_start).count();
        plugin.free_strategy(strategy);
        ::dlclose(library);
        std::cout << std::setprecision(17)
                  << "{\"prefix_seconds\":" << prefix_seconds
                  << ",\"stream_seconds\":" << stream_seconds
                  << ",\"prefix_bars\":" << prefix_bars
                  << ",\"stream_bars\":" << processed
                  << ",\"full_trades\":\"" << reference.back().trades << "\",\"rungs\":[";
        for (std::size_t rung = 0; rung < counts.size(); ++rung) {
            if (rung)
                std::cout << ',';
            std::cout << "{\"bars\":" << counts[rung]
                      << ",\"trades_equal\":" << (trades_equal[rung] ? "true" : "false")
                      << ",\"prefix_objective\":" << reference[rung].objective
                      << ",\"stream_objective\":" << stream_objectives[rung]
                      << ",\"objective_equal\":"
                      << (objectives_equal[rung] ? "true" : "false") << '}';
        }
        std::cout << "]}\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
