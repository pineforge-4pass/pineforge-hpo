#include "../src/core/numeric_build.hpp"

#include <iostream>
#include <stdexcept>

int main() {
    try {
        pineforge::hpo::detail::require_portable_environment();
        const double first = 0x1.0000000000001p0;
        const double second = 0x1.ffffffffffffep-1;
        const double third = -1.0;
        const double arithmetic = pfh_math_contraction_canary(first, second, third);
        const double fused = pineforge::hpo::detail::math::fma(first, second, third);
        if (pfh_math_bits(arithmetic) != pfh_math_bits(0.0) ||
            pfh_math_bits(fused) != pfh_math_bits(-0x1p-104))
            throw std::runtime_error("C-kernel implicit FMA contraction is enabled");
        std::cout << "PASS C-kernel contraction canary: separate multiply/add, explicit FMA\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
