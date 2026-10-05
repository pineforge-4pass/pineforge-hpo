#include "symbol_feeds.hpp"
#include <pineforge/hpo/trial_executor.hpp>

#include <filesystem>
#include <future>
#include <iostream>
#include <unistd.h>

namespace pfh = pineforge::hpo;
namespace detail = pineforge::hpo::detail;

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

int main(int argc, char** argv) {
    const auto directory = std::filesystem::temp_directory_path() /
                           ("pfh-feeds-" + std::to_string(::getpid()));
    try {
        require(argc == 3, "expected complete and legacy strategy plugins");
        std::filesystem::create_directories(directory);
        const auto csv = directory / "feed.csv";
        std::ofstream(csv) << "timestamp,open,high,low,close,volume\n"
                             "1700000000000,10,11,9,10,\n"
                             "1700000060000,20,21,19,20,NaN\n";
        const auto index = directory / "index.json";
        const std::string document = R"({"symbols":{"BINANCE:ETHUSDT":{
            "syminfo":{"tickerid":"BINANCE:ETHUSDT","type":"crypto","mintick":0.01},
            "feeds":{"1":"feed.csv"}}}})";
        std::ofstream(index) << document;
        const auto symbols = std::make_shared<const pfh::SymbolFeeds>(
            detail::load_symbol_feeds(index));
        require(symbols->size() == 1 && symbols->front().feeds.front().bars.size() == 2,
                "index did not load both bars");
        require(std::isnan(symbols->front().feeds.front().bars.front().volume),
                "missing volume is not na");
        const auto record = detail::symbol_feeds_record(*symbols);
        require(detail::field(detail::field(record, "symbols"), "BINANCE:ETHUSDT")
                    .find("facts") != nullptr, "facts missing from fingerprint");
        std::vector<pf_bar_t> chart(2);
        for (std::size_t index = 0; index < chart.size(); ++index) {
            chart[index].open = chart[index].high = chart[index].low = chart[index].close = 1;
            chart[index].volume = 1;
            chart[index].timestamp = static_cast<std::int64_t>(index) * 60000;
        }
        auto dataset = std::make_shared<const pfh::Dataset>(std::move(chart));
        auto plugin = std::make_shared<const pfh::StrategyPlugin>(argv[1]);
        pfh::BacktestConfiguration config;
        config.input_timeframe = "1";
        config.script_timeframe = "1";
        config.bar_magnifier = true;
        config.magnifier_samples = 6;
        config.magnifier_distribution = PF_MAGNIFIER_TRIANGLE;
        config.symbol_feeds = symbols;
        pfh::TrialExecutor executor(plugin, dataset, config);
        std::filesystem::remove(csv);
        std::filesystem::remove(index);
        for (int candidate = 1; candidate <= 3; ++candidate) {
            const auto result = executor.execute({{"Length", std::to_string(candidate)}});
            require(result.succeeded(), result.error);
            require(result.report.net_profit == candidate + 130,
                    "fresh trial did not receive the preloaded feed");
        }
        std::vector<std::future<pfh::TrialExecutionResult>> workers;
        for (int candidate = 1; candidate <= 8; ++candidate) {
            workers.push_back(std::async(std::launch::async, [&, candidate] {
                return executor.execute({{"Length", std::to_string(candidate)}});
            }));
        }
        for (std::size_t candidate = 0; candidate < workers.size(); ++candidate) {
            const auto result = workers[candidate].get();
            require(result.succeeded() && result.report.net_profit == candidate + 131,
                    "parallel trial changed immutable feeds");
        }
        bool refused = false;
        try {
            pfh::TrialExecutor legacy(std::make_shared<const pfh::StrategyPlugin>(argv[2]),
                                      dataset, config);
        } catch (const std::runtime_error& error) {
            refused = std::string(error.what()).find("--symbol-feeds:") == 0;
        }
        require(refused, "missing setter did not fail initialization");
        config.script_timeframe = "5";
        refused = false;
        try {
            pfh::TrialExecutor aggregated(plugin, dataset, config);
        } catch (const std::invalid_argument& error) {
            refused = std::string(error.what()).find("chart's own bars") != std::string::npos;
        }
        require(refused, "aggregated chart did not fail initialization");
        for (const auto* bad : {
                 R"({"symbols":{"E":{"feeds":{"4h":"missing.csv"}}}})",
                 R"({"symbols":{"E":{"feeds":{"D":"missing.csv","1D":"missing.csv"}}}})",
                 R"({"symbols":{"E":{"syminfo":{"mintick":false}}}})",
                 R"({"symbols":{"E":{"syminfo":{"mintick":0}}}})",
                 R"({"symbols":{"E":{"syminfo":{"timezone":5}}}})",
                 R"({"symbols":{"E":{"feeds":[]}}})",
                 R"({"symbols":{"E":{"feed":{}}}})",
                 R"({"symbols":{"E\u0000":{"feeds":{}}}})",
                 R"({"symbols":[]})"}) {
            refused = false;
            try {
                detail::load_symbol_feeds(detail::parse_json(bad), directory);
            } catch (const std::invalid_argument& error) {
                refused = std::string(error.what()).find("--symbol-feeds:") == 0;
            }
            require(refused, std::string("invalid index accepted: ") + bad);
        }
        require(detail::symbol_bar_close(1706659200000LL, "1M") == 1709164800000LL,
                "calendar month did not clamp January 31 to leap February 29");
        require(detail::symbol_double("１２.٥") == 12.5 &&
                    detail::symbol_integer("\u00a0１７０_０００\u2007") == 170000,
                "Unicode decimal digits or whitespace differ from the harness");
        require(detail::symbol_numeric_cell("\x1c\x1d", true).empty(),
                "optional numeric cells did not mirror Python strip");
        refused = false;
        try {
            detail::symbol_double("\x1c" "12.5");
        } catch (const std::invalid_argument&) {
            refused = true;
        }
        require(refused, "mandatory numeric cells accepted control separators");
        require(detail::load_symbol_feeds(detail::parse_json(R"({"symbols":{}})"), directory)
                    .empty(), "empty index is not a no-op");
        const auto tiny = detail::load_symbol_feeds(detail::parse_json(
            R"({"symbols":{"E":{"syminfo":{"mintick":5e-324}}}})"), directory);
        require(std::get<double>(tiny.front().facts.front().value) ==
                    std::numeric_limits<double>::denorm_min(),
                "positive subnormal mintick was refused or rounded");
        const auto tiny_record = detail::symbol_feeds_record(tiny);
        require(!detail::symbol_feeds_identity(&tiny_record).empty(),
                "subnormal mintick identity failed");
        auto malformed = detail::parse_json(
            R"({"symbols":{"E":{"syminfo":{"currency":"USDT"}}}})");
        malformed.members["symbols"].members["E"].members["syminfo"]
            .members["currency"] = detail::Json::string(std::string(1, '\xff'));
        refused = false;
        try {
            detail::load_symbol_feeds(malformed, directory);
        } catch (const std::invalid_argument& error) {
            refused = std::string(error.what()).find("E: syminfo.currency") != std::string::npos;
        }
        require(refused, "invalid fact UTF-8 did not name the symbol and fact");
        std::filesystem::remove_all(directory);
        std::cout << "PASS load once, immutable threaded trials, validation, calendar closes\n";
        return 0;
    } catch (const std::exception& error) {
        std::filesystem::remove_all(directory);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
