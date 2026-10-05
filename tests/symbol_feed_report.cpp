#include "symbol_feeds.hpp"
#include <pineforge/hpo/trial_executor.hpp>

#include <fstream>
#include <iostream>

namespace pfh = pineforge::hpo;

int main(int argc, char** argv) {
    try {
        if (argc != 6)
            throw std::invalid_argument("expected plugin, chart CSV, index, candidate, output dir");
        auto plugin = std::make_shared<const pfh::StrategyPlugin>(argv[1]);
        auto chart = std::make_shared<const pfh::Dataset>(pfh::Dataset::load_csv(argv[2]));
        pfh::BacktestConfiguration config;
        config.input_timeframe = "240";
        config.script_timeframe = "240";
        config.chart_timezone = "UTC";
        config.capture_trades = true;
        config.symbol_info = pfh::SymbolInfo{};
        config.symbol_info->mintick = 0.01;
        config.symbol_feeds = std::make_shared<const pfh::SymbolFeeds>(
            pfh::detail::load_symbol_feeds(std::filesystem::path(argv[3])));
        const pfh::TrialExecutor executor(plugin, chart, config);
        const auto result = executor.execute({{"ETH threshold", argv[4]},
                                              {"Other", "BINANCE:ETHUSDT"}});
        if (!result.succeeded())
            throw std::runtime_error(result.error);
        const auto directory = std::filesystem::path(argv[5]);
        std::filesystem::create_directories(directory);
        std::ofstream metrics(directory / "metrics.bin", std::ios::binary);
        metrics.write(reinterpret_cast<const char*>(&result.report.metrics),
                      sizeof(result.report.metrics));
        std::ofstream trades(directory / "trades.bin", std::ios::binary);
        trades.write(reinterpret_cast<const char*>(result.report.trades.data()),
                     static_cast<std::streamsize>(
                         result.report.trades.size() * sizeof(pf_trade_t)));
        if (!metrics || !trades)
            throw std::runtime_error("failed writing C-ABI records");
        std::cout << result.report.total_trades << '\n';
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
