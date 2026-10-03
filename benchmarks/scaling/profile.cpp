#include <pineforge/hpo/sampler.hpp>
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <string>
#include <sys/resource.h>
#include <vector>

namespace pfh = pineforge::hpo;

int main(int argc, char** argv) {
    if (argc != 4 && argc != 5)
        return 1;
    const auto dimensions = std::stoul(argv[1]);
    const auto history = std::stoull(argv[2]);
    const auto repetitions = std::stoull(argv[3]);
    const bool updating = argc == 5 && std::string(argv[4]) == "update";
    std::vector<pfh::Dimension> descriptors;
    for (unsigned dimension = 0; dimension < dimensions; ++dimension)
        descriptors.emplace_back(pfh::RealDimension("x" + std::to_string(dimension), -5, 5));
    pfh::TpeSamplerConfig config;
    config.startup_trials = history;
    pfh::TpeSampler sampler(pfh::SearchSpace(std::move(descriptors)), 17,
                            pfh::ObjectiveDirection::Minimize, 0, config);
    const auto started = std::chrono::steady_clock::now();
    for (std::uint64_t trial = 0; trial < history; ++trial) {
        auto candidate = sampler.ask();
        double objective = 0;
        for (const auto& parameter : candidate->values) {
            const auto value = std::get<double>(parameter.second);
            objective += value * value;
        }
        sampler.tell(candidate->id, objective);
    }
    const auto loaded = std::chrono::steady_clock::now();
    double ask_seconds = 0;
    double tell_seconds = 0;
    std::vector<std::pair<std::uint64_t, double>> pending;
    for (std::uint64_t attempt = 0; attempt < repetitions; ++attempt) {
        const auto begin = std::chrono::steady_clock::now();
        auto candidate = sampler.ask();
        ask_seconds += std::chrono::duration<double>(std::chrono::steady_clock::now() - begin)
                           .count();
        if (updating) {
            double objective = 0;
            for (const auto& parameter : candidate->values) {
                const double value = std::get<double>(parameter.second);
                objective += value * value;
            }
            pending.emplace_back(candidate->id, objective);
            if (pending.size() == 8 || attempt + 1 == repetitions) {
                const auto tell_begin = std::chrono::steady_clock::now();
                for (const auto& observation : pending)
                    sampler.tell(observation.first, observation.second);
                tell_seconds += std::chrono::duration<double>(
                    std::chrono::steady_clock::now() - tell_begin).count();
                pending.clear();
            }
        } else {
            sampler.abandon(candidate->id);
        }
    }
    struct rusage usage {};
    getrusage(RUSAGE_SELF, &usage);
    std::cout << std::setprecision(10) << "{\"dims\":" << dimensions
              << ",\"history\":" << history << ",\"ask_us\":"
              << ask_seconds * 1e6 / repetitions << ",\"populate_s\":"
              << std::chrono::duration<double>(loaded - started).count()
              << ",\"mode\":\"" << (updating ? "batch8_updates" : "snapshot") << "\""
              << ",\"tell_us\":" << tell_seconds * 1e6 / repetitions
              << ",\"retained_observations\":"
#if defined(PINEFORGE_HPO_LEGACY_TPE)
              << "null"
#else
              << sampler.retained_observations()
#endif
              << ",\"peak_rss_kib\":" << usage.ru_maxrss << "}\n";
}
