#include "symbol_feeds.hpp"
#include <pineforge/hpo/trial_executor.hpp>

#include <array>
#include <cstdint>
#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace pfh = pineforge::hpo;

namespace {

void refuse_named(const std::filesystem::path& path) {
    std::vector<std::string> parts;
    for (const auto& part : path)
        parts.push_back(part.string());
    for (std::size_t index = 0; index < parts.size(); ++index) {
        const auto& part = parts[index];
        if (part == ".ssh" || part == ".aws" || part == ".codex" ||
            (part.size() >= 4 && part.compare(part.size() - 4, 4, ".env") == 0) ||
            (part == ".config" && index + 1 < parts.size() &&
             (parts[index + 1] == "pineforge" ||
              (parts[index + 1] == "zsh" && index + 2 < parts.size() &&
               parts[index + 2] == "keys"))))
            throw std::runtime_error("refused sensitive path by name: " + path.string());
    }
}

std::filesystem::path checked_path(const std::filesystem::path& path) {
    std::cerr << "will follow: " << path << '\n';
    refuse_named(path);
    const auto resolved = std::filesystem::weakly_canonical(std::filesystem::absolute(path));
    refuse_named(resolved);
    return resolved;
}

pfh::SymbolFeeds checked_feeds(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input)
        throw std::runtime_error("cannot read feed index");
    std::ostringstream text;
    text << input.rdbuf();
    const auto document = pfh::detail::parse_json(text.str());
    if (const auto* symbols = document.find("symbols")) {
        for (const auto& symbol : symbols->members) {
            if (const auto* feeds = symbol.second.find("feeds")) {
                for (const auto& feed : feeds->members)
                    checked_path(path.parent_path() / feed.second.value);
            }
        }
    }
    return pfh::detail::load_symbol_feeds(document, path.parent_path());
}

class ArenaStats final {
public:
    explicit ArenaStats(const std::filesystem::path& path) {
        library_ = ::dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (!library_)
            throw std::runtime_error("cannot open real plugin for arena diagnostics");
        getter_ = reinterpret_cast<Getter>(::dlsym(library_, "pf_report_scratch_arena_stats"));
        if (!getter_) {
            ::dlclose(library_);
            throw std::runtime_error("real plugin lacks pf_report_scratch_arena_stats");
        }
    }
    ~ArenaStats() { ::dlclose(library_); }
    std::array<std::uint64_t, 2> read() const {
        std::array<std::uint64_t, 2> values{};
        if (getter_(&values[0], &values[1]) != 0)
            throw std::runtime_error("arena diagnostics query failed");
        return values;
    }

private:
    using Getter = int (*)(std::uint64_t*, std::uint64_t*);
    void* library_ = nullptr;
    Getter getter_ = nullptr;
};

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc != 6 && (argc != 7 || std::string(argv[6]) != "--arena-stats"))
            throw std::invalid_argument("expected plugin, chart CSV, index, candidate, output dir");
        const auto plugin_path = checked_path(argv[1]);
        const auto chart_path = checked_path(argv[2]);
        const auto index_path = checked_path(argv[3]);
        const auto directory = checked_path(argv[5]);
        auto feeds = checked_feeds(index_path);
        auto plugin = std::make_shared<const pfh::StrategyPlugin>(plugin_path);
        auto chart = std::make_shared<const pfh::Dataset>(pfh::Dataset::load_csv(chart_path));
        const auto stats = argc == 7 ? std::make_unique<ArenaStats>(plugin_path) : nullptr;
        const auto before = stats ? stats->read() : std::array<std::uint64_t, 2>{};
        pfh::BacktestConfiguration config;
        config.input_timeframe = "240";
        config.script_timeframe = "240";
        config.chart_timezone = "UTC";
        config.capture_trades = true;
        config.symbol_info = pfh::SymbolInfo{};
        config.symbol_info->mintick = 0.01;
        config.symbol_feeds = std::make_shared<const pfh::SymbolFeeds>(std::move(feeds));
        const pfh::TrialExecutor executor(plugin, chart, config);
        const auto preflight = stats ? stats->read() : std::array<std::uint64_t, 2>{};
        const auto result = executor.execute({{"ETH threshold", argv[4]},
                                              {"Other", "BINANCE:ETHUSDT"}});
        if (!result.succeeded())
            throw std::runtime_error(result.error);
        std::filesystem::create_directories(directory);
        std::ofstream metrics(checked_path(directory / "metrics.bin"), std::ios::binary);
        metrics.write(reinterpret_cast<const char*>(&result.report.metrics),
                      sizeof(result.report.metrics));
        std::ofstream trades(checked_path(directory / "trades.bin"), std::ios::binary);
        trades.write(reinterpret_cast<const char*>(result.report.trades.data()),
                     static_cast<std::streamsize>(
                         result.report.trades.size() * sizeof(pf_trade_t)));
        if (!metrics || !trades)
            throw std::runtime_error("failed writing C-ABI records");
        if (stats) {
            const auto after = stats->read();
            std::ofstream output(checked_path(directory / "arena-stats.json"));
            output << "{\"before_high_water\":" << before[0]
                   << ",\"before_bytes\":" << before[1]
                   << ",\"preflight_high_water\":" << preflight[0]
                   << ",\"preflight_bytes\":" << preflight[1]
                   << ",\"after_high_water\":" << after[0]
                   << ",\"after_bytes\":" << after[1] << "}\n";
            if (!output)
                throw std::runtime_error("failed writing arena diagnostics");
        }
        std::cout << result.report.total_trades << '\n';
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
