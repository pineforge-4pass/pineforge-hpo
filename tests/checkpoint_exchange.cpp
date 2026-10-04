#include <pineforge/hpo/sampler.hpp>

#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>

namespace pfh = pineforge::hpo;

int main(int argc, char** argv) {
    try {
        if (argc != 3)
            throw std::invalid_argument("checkpoint_exchange write|restore FILE");
        const pfh::SearchSpace space({pfh::RealDimension("x", 0.0, 1.0)});
        pfh::TpeSamplerConfig config;
        config.history_switch = 32;
        config.bad_reservoir_size = 7;
        std::vector<pfh::WarmStartObservation> warm;
        for (std::uint64_t row = 0; row < 201; ++row) {
            pfh::Candidate candidate;
            candidate.id = row;
            candidate.values = {{"x", static_cast<double>(row % 97) / 97}};
            warm.push_back({candidate, static_cast<double>(row % 31)});
        }
        pfh::TpeSampler sampler(space, 17, pfh::ObjectiveDirection::Maximize, 0, config);
        if (std::string(argv[1]) == "write") {
            sampler.warm_start(warm);
            std::ofstream output(argv[2]);
            output << sampler.sampler_state();
            if (!output)
                throw std::runtime_error("checkpoint write failed");
            std::cout << "wrote canonical bounded PFHTPE2\n";
        } else {
            std::ifstream input(argv[2]);
            if (!input)
                throw std::runtime_error("checkpoint read failed");
            const std::string state(std::istreambuf_iterator<char>(input), {});
            const bool restored = sampler.warm_start(warm, 5, state);
            if (!sampler.ask())
                throw std::runtime_error("restored/rebuilt sampler produced no proposal");
            std::cout << (restored ? "restored_sampler_state" : "rebuilt_history") << '\n';
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 4;
    }
}
