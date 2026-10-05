#include <pineforge/hpo/sampler.hpp>

#include <chrono>
#include <iostream>
#include <map>
#include <stdexcept>

namespace pfh = pineforge::hpo;

int main(int argc, char** argv) {
    try {
        const auto seed = argc == 2 ? std::stoull(argv[1]) : 170905;
        const std::map<std::string, pfh::SearchSpace> spaces = {
            {"linear-real", pfh::SearchSpace({pfh::RealDimension("x", -10.0, 10.0)})},
            {"log-real", pfh::SearchSpace({pfh::RealDimension("x", 0.001, 1000.0,
                                                            std::nullopt, true)})},
            {"stepped-real", pfh::SearchSpace({pfh::RealDimension("x", -10.0, 10.0, 0.25)})},
            {"linear-int", pfh::SearchSpace({pfh::IntegerDimension("x", -1000, 1000)})},
            {"log-int", pfh::SearchSpace({pfh::IntegerDimension("x", 1, 10000, 1, true)})},
            {"categorical", pfh::SearchSpace({pfh::CategoricalDimension("x",
                                             {std::string("a"), std::string("b")})})},
            {"bool", pfh::SearchSpace({pfh::BooleanDimension("x")})},
            {"mixed", pfh::SearchSpace({pfh::RealDimension("real", -10.0, 10.0),
                pfh::RealDimension("log", 0.001, 1000.0, std::nullopt, true),
                pfh::RealDimension("step", -10.0, 10.0, 0.25),
                pfh::IntegerDimension("int", -1000, 1000),
                pfh::IntegerDimension("logint", 1, 10000, 1, true),
                pfh::CategoricalDimension("category", {std::string("a"), std::string("b")}),
                pfh::BooleanDimension("bool")})}};
        for (const auto& entry : spaces) {
            pfh::TpeSamplerConfig config;
            config.max_threads = 1;
            pfh::TpeSampler sampler(entry.second, seed, pfh::ObjectiveDirection::Minimize,
                                    0, config);
            std::chrono::nanoseconds elapsed{0};
            for (std::uint64_t trial = 0; trial < 768; ++trial) {
                const auto start = std::chrono::steady_clock::now();
                const auto candidate = sampler.ask();
                const auto finish = std::chrono::steady_clock::now();
                if (!candidate)
                    throw std::runtime_error("sampler unexpectedly exhausted");
                if (trial >= 512)
                    elapsed += finish - start;
                sampler.tell(candidate->id, static_cast<double>((trial * 7919) % 10007));
            }
            std::cout << entry.first << ',' << seed << ',' << elapsed.count() << ",256\n";
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
