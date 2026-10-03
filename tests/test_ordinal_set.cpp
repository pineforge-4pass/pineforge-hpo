#include "ordinal_set.hpp"

#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>

int main() {
    try {
        pineforge::hpo::detail::OrdinalSet ordinals;
        pineforge::hpo::detail::OrdinalSet dense(100000000);
        for (const auto value : {std::uint64_t{0}, std::uint64_t{63}, std::uint64_t{64},
                                 std::uint64_t{99999999}}) {
            if (!dense.insert(value) || dense.insert(value) || !dense.contains(value))
                throw std::runtime_error("dense coverage lost a boundary ordinal");
        }
        if (dense.size() != 4 || dense.contains(100000000))
            throw std::runtime_error("dense coverage count or range is incorrect");
        bool range_rejected = false;
        try {
            dense.insert(100000000);
        } catch (const std::out_of_range&) {
            range_rejected = true;
        }
        if (!range_rejected)
            throw std::runtime_error("dense coverage accepted an out-of-range ordinal");
        dense.clear();
        if (dense.size() != 0 || dense.contains(64) || !dense.insert(64))
            throw std::runtime_error("dense coverage reset retained state");
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
