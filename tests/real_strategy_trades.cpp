#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>

#include <iomanip>
#include <iostream>
#include <locale>
#include <stdexcept>

namespace pfh = pineforge::hpo;

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

int main(int argc, char** argv) {
    try {
        if (argc != 3)
            throw std::invalid_argument("real_strategy_trades PLUGIN OHLCV");
        const pfh::StrategyPlugin plugin(argv[1]);
        const auto dataset = pfh::Dataset::load_csv(argv[2]);
        Trial trial(plugin);
        plugin.set_input(trial.strategy, "Entry Level", "101");
        plugin.set_input(trial.strategy, "Exit Level", "98");
        plugin.run_backtest_full(trial.strategy, dataset.data(),
            static_cast<int>(dataset.size()), "1", "1", false, 4, PF_MAGNIFIER_ENDPOINTS,
            &trial.report);
        const auto error = plugin.last_error(trial.strategy);
        if (!error.empty())
            throw std::runtime_error(error);
        std::cout.imbue(std::locale::classic());
        std::cout << std::setprecision(17) << std::showpoint << std::boolalpha << '[';
        for (std::int32_t index = 0; index < trial.report.total_trades; ++index) {
            const auto& trade = trial.report.trades[index];
            if (index != 0)
                std::cout << ',';
            std::cout << "{\"n\":" << index + 1
                << ",\"side\":\"" << (trade.is_long ? "long" : "short") << '"'
                << ",\"entry_time\":" << trade.entry_time
                << ",\"exit_time\":" << trade.exit_time
                << ",\"entry_price\":" << trade.entry_price
                << ",\"exit_price\":" << trade.exit_price
                << ",\"qty\":" << trade.qty
                << ",\"pnl\":" << trade.pnl
                << ",\"pnl_pct\":" << trade.pnl_pct
                << ",\"max_runup\":" << trade.max_runup
                << ",\"max_drawdown\":" << trade.max_drawdown
                << ",\"commission\":" << trade.commission
                << ",\"entry_bar_index\":" << trade.entry_bar_index
                << ",\"exit_bar_index\":" << trade.exit_bar_index
                << ",\"open_at_end\":" << (trade.open_at_end != 0) << '}';
        }
        std::cout << "]\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
