#include "ordinal_set.hpp"

#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>

int main() {
    try {
        pineforge::hpo::detail::OrdinalSet ordinals;
        const auto maximum = std::numeric_limits<std::uint64_t>::max();
        if (ordinals.contains(0) || ordinals.size() != 0)
            throw std::runtime_error("new index is not empty");
        for (std::uint64_t index = 0; index < 4096; ++index) {
            if (!ordinals.insert(index * 1000000007) || ordinals.insert(index * 1000000007))
                throw std::runtime_error("index lost uniqueness while growing");
        }
        if (!ordinals.insert(maximum) || ordinals.size() != 4097)
            throw std::runtime_error("index lost full-width ordinal");
        for (std::uint64_t index = 0; index < 4096; ++index) {
            if (!ordinals.contains(index * 1000000007))
                throw std::runtime_error("index lost an ordinal after growing");
        }
        if (!ordinals.contains(maximum) || ordinals.contains(1))
            throw std::runtime_error("index returned incorrect membership");
        ordinals.clear();
        if (ordinals.size() != 0 || ordinals.contains(0) || ordinals.contains(maximum) ||
            !ordinals.insert(maximum))
            throw std::runtime_error("index reset retained state");
        std::cout << "disk ordinal growth, uniqueness, full-width keys, and reset passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
