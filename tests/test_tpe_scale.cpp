#include <pineforge/hpo/sampler.hpp>

#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <sys/resource.h>
#include <vector>

namespace pfh = pineforge::hpo;
using Clock = std::chrono::steady_clock;

int main() {
    try {
        std::vector<pfh::Dimension> dimensions;
        for (unsigned index = 0; index < 8; ++index)
            dimensions.emplace_back(pfh::RealDimension("x" + std::to_string(index), -5, 5));
        pfh::TpeSampler sampler(pfh::SearchSpace(dimensions), 17,
                                pfh::ObjectiveDirection::Minimize);
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
        for (std::uint64_t trial = 0; trial < 100000; ++trial) {
            const auto begin = Clock::now();
            auto candidate = sampler.ask();
            const double seconds = std::chrono::duration<double>(Clock::now() - begin).count();
            if (trial >= 1000 && trial < 2000)
                early_seconds += seconds;
            if (trial >= 99000)
                late_seconds += seconds;
            double objective = 0.0;
            for (const auto& parameter : candidate->values) {
                const double value = std::get<double>(parameter.second);
                objective += value * value;
            }
            sampler.tell(candidate->id, objective);
            if (trial >= 1000 && sampler.retained_observations() > 537)
                throw std::runtime_error("TPE retained unbounded history");
            if (trial == 2000) {
                struct rusage usage {};
                getrusage(RUSAGE_SELF, &usage);
                initial_rss = usage.ru_maxrss;
            }
        }
        struct rusage usage {};
        getrusage(RUSAGE_SELF, &usage);
#if defined(__linux__)
        if (usage.ru_maxrss - initial_rss > 65536)
            throw std::runtime_error("TPE resident memory grew by more than 64 MiB");
#endif
        if (late_seconds > early_seconds * 5 + 0.05)
            throw std::runtime_error("TPE ask cost grew with history");
        if (late_seconds > 5.0)
            throw std::runtime_error("TPE ask cost exceeded 5 ms");
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
        std::cout << "100000 trials: early ask " << early_seconds * 1000
                  << " us; late ask " << late_seconds * 1000 << " us; RSS "
                  << usage.ru_maxrss << " KiB\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
