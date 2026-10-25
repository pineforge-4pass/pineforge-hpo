#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/strategy_plugin.hpp>
#include <pineforge/hpo/trial_executor.hpp>

#include <dlfcn.h>

#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace {

namespace pfh = pineforge::hpo;

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

std::filesystem::path checked_plugin(const std::filesystem::path& path) {
    const auto check = [](const std::filesystem::path& candidate) {
        std::vector<std::string> parts;
        for (const auto& part : candidate) {
            const auto name = part.string();
            require(name != ".ssh" && name != ".aws" && name != ".codex" &&
                        !(name.size() >= 4 && name.compare(name.size() - 4, 4, ".env") == 0),
                    "refused protected path by name: " + candidate.string());
            parts.push_back(name);
        }
        for (std::size_t index = 0; index < parts.size(); ++index) {
            if (parts[index] != ".config")
                continue;
            require(index + 1 == parts.size() || parts[index + 1] != "pineforge",
                    "refused protected path by name: " + candidate.string());
            require(index + 2 >= parts.size() || parts[index + 1] != "zsh" ||
                        parts[index + 2] != "keys",
                    "refused protected path by name: " + candidate.string());
        }
    };
    check(path);
    const auto absolute = std::filesystem::absolute(path).lexically_normal();
    check(absolute);
    const auto resolved = std::filesystem::weakly_canonical(absolute);
    check(resolved);
    std::cout << "FILES plugin read: " << resolved << '\n' << std::flush;
    return resolved;
}

template <typename Function>
void require_typed(Function function, const std::string& code) {
    try {
        function();
    } catch (const pfh::TypedHpoError<std::invalid_argument>& error) {
        require(error.code() == code, "unexpected view failure code: " + error.code());
        return;
    }
    throw std::runtime_error("view operation did not throw a typed invalid_argument");
}

std::vector<pf_bar_t> fixture() {
    std::vector<pf_bar_t> bars;
    for (std::int64_t value = 1; value <= 9; ++value) {
        const auto price = static_cast<double>(value);
        bars.push_back({price, price + 2, price - 1, price + 1, price + 3,
                        value * 1'000'000'000'000LL});
    }
    return bars;
}

void check_views() {
    static_assert(std::is_same_v<decltype(std::declval<const pfh::DatasetView&>().data()),
                                 const pf_bar_t*>);
    auto owner = std::make_shared<const pfh::Dataset>(fixture());
    const pfh::DatasetView full(owner);
    const pfh::DatasetView middle(owner, 2, 4);
    const pfh::DatasetView empty(owner, owner->size(), 0);
    require(full.data() == owner->data(), "whole view changed the bar pointer");
    require(full.size() == owner->size(), "whole view changed the length");
    require(middle.data() == owner->data() + 2 && middle.size() == 4,
            "view copied bars or lost its offset");
    require(empty.empty() && empty.data() == owner->data() + owner->size(),
            "explicit empty tail view is not a valid one-past-end range");
    require_typed([&] { pfh::DatasetView invalid(owner, 8, 2); }, "hpo_dataset_invalid");
    require_typed([&] { pfh::DatasetView invalid(owner, 10, 0); }, "hpo_dataset_invalid");
    require_typed([&] {
        pfh::DatasetView invalid(owner, 1, std::numeric_limits<std::size_t>::max());
    }, "hpo_dataset_invalid");
    require_typed([&] {
        pfh::DatasetView invalid(owner, std::numeric_limits<std::size_t>::max(), 2);
    }, "hpo_dataset_invalid");
    require_typed([] { pfh::DatasetView invalid(nullptr, 0, 0); }, "hpo_dataset_invalid");
    const auto blank = std::make_shared<const pfh::Dataset>(std::vector<pf_bar_t>{});
    const pfh::DatasetView blank_view(blank, 0, 0);
    require(blank_view.empty() && blank_view.data() == blank->data(),
            "empty owner view changed its pointer");
    std::weak_ptr<const pfh::Dataset> lifetime;
    {
        const auto retained = [&] {
            auto temporary = std::make_shared<const pfh::Dataset>(fixture());
            lifetime = temporary;
            return pfh::DatasetView(temporary, 2, 4);
        }();
        require(!lifetime.expired() && retained.data()[0].close == 4,
                "view did not retain its immutable owner");
    }
    require(lifetime.expired(), "view kept its owner alive after the last view was destroyed");
}

void check_execution(const std::string& plugin_path) {
    auto plugin = std::make_shared<const pfh::StrategyPlugin>(plugin_path);
    void* observer = ::dlopen(plugin_path.c_str(), RTLD_NOW | RTLD_LOCAL);
    require(observer != nullptr, "cannot open fixture pointer observer");
    const auto last_bars = reinterpret_cast<const pf_bar_t* (*)()>(
        ::dlsym(observer, "fake_last_bars"));
    require(last_bars != nullptr, "fixture has no bar-pointer observation");
    auto owner = std::make_shared<const pfh::Dataset>(fixture());
    const pfh::DatasetView view(owner, 2, 4);
    auto copied = std::make_shared<const pfh::Dataset>(
        std::vector<pf_bar_t>(owner->data() + 2, owner->data() + 6));
    pfh::BacktestConfiguration configuration;
    configuration.input_timeframe = "1";
    configuration.script_timeframe = "5";
    configuration.bar_magnifier = true;
    configuration.magnifier_samples = 6;
    configuration.magnifier_distribution = PF_MAGNIFIER_TRIANGLE;
    const pfh::ParameterValues inputs{
        {"Length", "3"}, {"BatchPrefixTest", "1"}, {"BatchPrefixJitter", "0"},
        {"ChartBarsTest", "1"}};
    const pfh::TrialExecutor sliced(plugin, view, configuration);
    const auto sliced_result = sliced.execute(inputs);
    require(sliced_result.succeeded(), "view execution failed: " + sliced_result.error);
    require(last_bars() == view.data(), "executor copied bars or passed the owner's start");
    const pfh::TrialExecutor detached(plugin, pfh::DatasetView(copied), configuration);
    const auto copied_result = detached.execute(inputs);
    require(copied_result.succeeded(), "copied-subset execution failed");
    // Bars v=3..6 each contribute 16*v+20; base=100 and Length=3.
    const double expected = 100 + 3 + 16 * (3 + 4 + 5 + 6) + 4 * 20;
    require(sliced_result.report.net_profit == expected, "view score disagrees with arithmetic");
    require(copied_result.report.net_profit == expected, "copied-subset score is wrong");
    require(sliced_result.report.input_bars_processed == 4,
            "executor ran beyond the view length");
    require_typed([&] { (void)sliced.execute_prefix(inputs, {}, 5); }, "hpo_invariant");
    require_typed([&] { (void)sliced.execute_prefix(inputs, {}, 0); }, "hpo_invariant");
    const pfh::TrialExecutor empty(plugin, pfh::DatasetView(owner, 9, 0), configuration);
    require_typed([&] { (void)empty.execute(inputs); }, "hpo_invariant");
    const pfh::TrialExecutor full(plugin, pfh::DatasetView(owner), configuration);
    const auto full_result = full.execute(inputs);
    require(full_result.succeeded() && last_bars() == owner->data(),
            "full-length execution changed its engine pointer");

    configuration.pbo_blocks = 2;
    try {
        const pfh::TrialExecutor blocks(plugin, view, configuration);
        auto block_inputs = inputs;
        block_inputs.emplace("CurveEquity", "1,2,1.5,1.5,1.875");
        block_inputs.emplace("PrefixCurveEquity", "1,2");
        block_inputs.emplace("FullWindowBars", "4");
        const auto prefix = blocks.execute_prefix(block_inputs, {}, 2);
        require(prefix.succeeded() &&
                    prefix.report.return_blocks.status == pfh::ReturnBlocksStatus::NotRequested,
                "prefix reduced full-view blocks");
        const auto completed = blocks.execute(block_inputs);
        require(completed.succeeded() &&
                    completed.report.return_blocks.status == pfh::ReturnBlocksStatus::Ok,
                "completed view did not reduce blocks (owner is longer)");
        // Returns (1, -1/4, 0, 1/4), in two blocks of two intervals.
        require(completed.report.return_blocks.count == std::vector<std::int64_t>({2, 2}) &&
                    completed.report.return_blocks.sum == std::vector<double>({0.75, 0.25}) &&
                    completed.report.return_blocks.m2 == std::vector<double>({0.78125, 0.03125}),
                "view block moments disagree with independent dyadic arithmetic");
    } catch (const pfh::HpoError& error) {
        const char* required = std::getenv("PFH_REQUIRE_RETURN_STATS_IDENTITY");
        if (error.code() != "hpo_toolchain_unavailable" ||
            std::string(error.what()).find("identity is unbound") == std::string::npos ||
            (required && std::string(required) == "1"))
            throw;
        std::cout << "view block case unavailable: return-statistics identity is unbound\n";
    }
    ::dlclose(observer);
}

}  // namespace

// Proves bounds, owner lifetime, no copying, the actual engine pointer, arithmetic and typed
// overrun refusal. False passes: comparing two wrong slices only, observing the view pointer
// without checking what the plugin received, or accepting an untyped exception as a refusal.
int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: test_dataset_view PLUGIN");
        const auto plugin = checked_plugin(argv[1]);
        check_views();
        check_execution(plugin.string());
        std::cout << "dataset view verdict: PASS\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
