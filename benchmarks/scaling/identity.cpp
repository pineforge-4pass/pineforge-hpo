#define main pinned_benchmark_main
#include "native_benchmark.cpp"
#undef main

int main(int argc, char** argv) {
    if (argc != 5)
        return 1;
    const auto problem = make_hard_problem(argv[1], 1);
    const std::uint64_t seed = std::stoull(argv[2]);
    const std::uint64_t trials = std::stoull(argv[3]);
    pfh::TpeSamplerConfig config;
#if !defined(PINEFORGE_HPO_LEGACY_TPE)
    config.history_switch = std::stoull(argv[4]);
#endif
    pfh::TpeSampler sampler(problem.space, seed, pfh::ObjectiveDirection::Minimize, 0, config);
    for (std::uint64_t first = 0; first < trials; first += 8) {
        std::vector<pfh::Candidate> batch;
        for (std::uint64_t index = first; index < std::min(first + 8, trials); ++index)
            batch.push_back(*sampler.ask());
        for (const auto& candidate : batch) {
            const double value = problem.objective(candidate);
            std::cout << candidate.id << ' ' << std::hexfloat << value;
            for (const auto& parameter : candidate.values) {
                std::cout << ' ' << std::quoted(parameter.first) << ':';
                std::visit([](const auto& typed) { std::cout << typed; }, parameter.second);
            }
            std::cout << '\n';
            sampler.tell(candidate.id, value);
        }
    }
    return 0;
}
