#include <pineforge/hpo/sampler.hpp>

#include "../src/core/tpe_test_hooks.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <sys/resource.h>
#include <vector>

namespace pfh = pineforge::hpo;
using Clock = std::chrono::steady_clock;

namespace {
std::uint64_t acquisition_scores = 0;

void count_acquisition_score(double) {
    ++acquisition_scores;
}
}  // namespace

int main() {
    try {
        std::vector<pfh::Dimension> dimensions;
        for (unsigned index = 0; index < 8; ++index)
            dimensions.emplace_back(pfh::RealDimension("x" + std::to_string(index), -5, 5));
        pfh::TpeSamplerConfig bounded_config;
        bounded_config.history_switch = 1000;
        pfh::TpeSampler sampler(pfh::SearchSpace(dimensions), 17,
                                pfh::ObjectiveDirection::Minimize, 0, bounded_config);
        pfh::TpeSamplerConfig exact_config;
        exact_config.startup_trials = 2048;
        if (exact_config.history_switch)
            throw std::runtime_error("TPE default history switch must be unset");
        pfh::TpeSampler exact(pfh::SearchSpace(dimensions), 17,
                              pfh::ObjectiveDirection::Minimize, 0, exact_config);
        for (std::uint64_t trial = 0; trial < 1024; ++trial) {
            auto candidate = exact.ask();
            exact.tell(candidate->id, static_cast<double>(trial));
        }
        if (exact.retained_observations() != 1024)
            throw std::runtime_error("TPE default did not retain full history");
        pfh::TpeSamplerConfig switch_config;
        switch_config.history_switch = 128;
        switch_config.startup_trials = 200;
        switch_config.bad_reservoir_size = 0;
        pfh::TpeSampler completed_switch(pfh::SearchSpace(dimensions), 17,
                                         pfh::ObjectiveDirection::Minimize, 0, switch_config);
        for (std::uint64_t trial = 0; trial < 256; ++trial) {
            auto candidate = completed_switch.ask();
            completed_switch.abandon(candidate->id);
        }
        for (std::uint64_t trial = 0; trial < 127; ++trial) {
            auto candidate = completed_switch.ask();
            completed_switch.tell(candidate->id, static_cast<double>(trial));
        }
        if (completed_switch.retained_observations() != 127)
            throw std::runtime_error("TPE switched on issued rather than completed trials");
        auto candidate_at_switch = completed_switch.ask();
        completed_switch.tell(candidate_at_switch->id, 128.0);
        if (completed_switch.retained_observations() != 89)
            throw std::runtime_error("TPE did not compact at the completed-history switch");
        double early_seconds = 0.0;
        double late_seconds = 0.0;
        long initial_rss = 0;
        const auto bounded_scores =
            std::min(bounded_config.ei_candidates, bounded_config.scale_ei_candidates);
        std::uint64_t early_scores = 0;
        std::uint64_t late_scores = 0;
        pfh::detail::set_tpe_log_ratio_observer(count_acquisition_score);
        for (std::uint64_t trial = 0; trial < 100000; ++trial) {
            acquisition_scores = 0;
            const auto begin = Clock::now();
            auto candidate = sampler.ask();
            const double seconds = std::chrono::duration<double>(Clock::now() - begin).count();
            // One observer call per scored proposal. Together with the retained-history
            // bound below, this checks bounded acquisition work without a clock limit.
            // Per-score cost is covered only by the informational timings printed below.
            if (trial >= 1000 && acquisition_scores != bounded_scores)
                throw std::runtime_error("TPE bounded ask changed acquisition work");
            if (trial >= 1000 && trial < 2000) {
                early_seconds += seconds;
                early_scores += acquisition_scores;
            }
            if (trial >= 99000) {
                late_seconds += seconds;
                late_scores += acquisition_scores;
            }
            double objective = 0.0;
            for (const auto& parameter : candidate->values) {
                const double value = std::get<double>(parameter.second);
                objective += value * value;
            }
            sampler.tell(candidate->id, objective);
            if (trial < 999 && sampler.retained_observations() != trial + 1)
                throw std::runtime_error(
                    "TPE compacted opted-in history before 1000 completions");
            if (trial == 999 && sampler.retained_observations() != 537)
                throw std::runtime_error(
                    "TPE did not switch opted-in history at 1000 completions");
            if (trial >= 999 && sampler.retained_observations() > 537)
                throw std::runtime_error("TPE retained unbounded history");
            if ((trial == 1000 || trial == 99999) && sampler.retained_observations() != 537)
                throw std::runtime_error("TPE bounded history changed size");
            if (sampler.completed() != trial + 1 || sampler.outstanding() != 0)
                throw std::runtime_error("TPE lost completed trials during scale test");
            if (trial == 2000) {
                struct rusage usage {};
                getrusage(RUSAGE_SELF, &usage);
                initial_rss = usage.ru_maxrss;
            }
        }
        pfh::detail::set_tpe_log_ratio_observer(nullptr);
        struct rusage usage {};
        getrusage(RUSAGE_SELF, &usage);
#if defined(__linux__)
        if (usage.ru_maxrss - initial_rss > 65536)
            throw std::runtime_error("TPE resident memory grew by more than 64 MiB");
#endif
        if (sampler.completed() != 100000 || sampler.outstanding() != 0)
            throw std::runtime_error("TPE lost completed trials");
        sampler.reset();
        if (sampler.retained_observations() != 0 || sampler.completed() != 0)
            throw std::runtime_error("TPE reset retained history");
        pfh::TpeSampler reference(pfh::SearchSpace(dimensions), 17);
        if (sampler.ask()->values != reference.ask()->values)
            throw std::runtime_error("TPE reset changed seed replay");
        dimensions.clear();
        for (unsigned index = 0; index < 64; ++index)
            dimensions.emplace_back(
                pfh::IntegerDimension("x" + std::to_string(index), 1, 1000000000));
        pfh::TpeSampler huge(pfh::SearchSpace(std::move(dimensions)), 17);
        if (huge.ask()->values.size() != 64)
            throw std::runtime_error("TPE rejected huge Cartesian space");
        pfh::TpeSampler fixed(pfh::SearchSpace({
            pfh::IntegerDimension("integer", 42, 42),
            pfh::RealDimension("real", 0.5, 0.5),
            pfh::CategoricalDimension("category", {"one"}),
            pfh::BooleanDimension("boolean")}), 17);
        for (std::uint64_t trial = 0; trial < 1100; ++trial) {
            auto candidate = fixed.ask();
            if (std::get<std::int64_t>(*candidate->find("integer")) != 42 ||
                std::get<double>(*candidate->find("real")) != 0.5)
                throw std::runtime_error("TPE changed fixed parameters");
            fixed.tell(candidate->id, 0.0);
        }
        std::cout << "100000 trials (informational timing): early ask " << early_seconds * 1000
                  << " us; late ask " << late_seconds * 1000 << " us; RSS "
                  << usage.ru_maxrss << " KiB\n";
        std::cout << "Bounded acquisition scores: early " << early_scores
                  << "; late " << late_scores << "; per ask " << bounded_scores << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
