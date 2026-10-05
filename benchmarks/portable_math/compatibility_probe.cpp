#include <pineforge/hpo/sampler.hpp>

#include <cstring>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <string>

namespace pfh = pineforge::hpo;

std::map<std::string, pfh::SearchSpace> spaces() {
    return {
        {"linear-real", pfh::SearchSpace({pfh::RealDimension("x", -100.0, 100.0)})},
        {"log-real", pfh::SearchSpace({
            pfh::RealDimension("x", 0x1p-1022, 0x1p1023, std::nullopt, true)})},
        {"stepped-real", pfh::SearchSpace({pfh::RealDimension("x", -12.0, 14.0, 0.1)})},
        {"decimal-endpoint", pfh::SearchSpace({pfh::RealDimension("x", 0.0, 0.3, 0.1)})},
        {"wide-grid", pfh::SearchSpace({pfh::RealDimension("x", -0x1p100, 0x1p100,
                                                          0x1p94)})},
        {"linear-int", pfh::SearchSpace({pfh::IntegerDimension("x", -9999, 9999, 3)})},
        {"log-int", pfh::SearchSpace({pfh::IntegerDimension("x", 1, 1000000, 1, true)})},
        {"categorical", pfh::SearchSpace({pfh::CategoricalDimension("x",
            {std::string("fast"), std::string("slow"), std::int64_t{7}, 0.25})})},
        {"bool", pfh::SearchSpace({pfh::BooleanDimension("x")})},
        {"mixed", pfh::SearchSpace({pfh::RealDimension("step", -10.0, 10.0, 0.25),
            pfh::IntegerDimension("int", -100, 100),
            pfh::IntegerDimension("logint", 1, 999, 1, true),
            pfh::CategoricalDimension("category", {std::string("a"), std::string("b")}),
            pfh::BooleanDimension("bool")})}
    };
}

int main() {
    for (const auto& entry : spaces()) {
        for (const std::string kind : {"random", "grid"}) {
            if (kind == "grid" && !entry.second.finite_cardinality())
                continue;
            std::unique_ptr<pfh::Sampler> sampler;
            if (kind == "grid")
                sampler = std::make_unique<pfh::GridSampler>(entry.second);
            else
                sampler = std::make_unique<pfh::RandomSampler>(entry.second, 170905, 256);
            std::cout << kind << ' ' << entry.first << '\n';
            for (unsigned trial = 0; trial < 256; ++trial) {
                const auto candidate = sampler->next();
                if (!candidate)
                    break;
                std::cout << candidate->id;
                for (const auto& parameter : candidate->values) {
                    std::cout << ' ' << std::quoted(parameter.first) << ' '
                              << parameter.second.index() << ' ';
                    if (const auto* value = std::get_if<double>(&parameter.second)) {
                        std::uint64_t bits;
                        std::memcpy(&bits, value, sizeof(bits));
                        std::cout << bits;
                    } else if (const auto* value = std::get_if<std::string>(&parameter.second)) {
                        std::cout << std::quoted(*value);
                    } else {
                        std::visit([](const auto& value) { std::cout << value; }, parameter.second);
                    }
                }
                std::cout << '\n';
            }
        }
    }
}
