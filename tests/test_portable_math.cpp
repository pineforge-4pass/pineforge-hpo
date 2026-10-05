#include "../src/core/numeric_build.hpp"
#include "../src/core/portable_grid.hpp"
#include "../src/core/tpe_test_hooks.hpp"

#include <array>
#include <cmath>
#include <iostream>
#include <random>
#include <stdexcept>

#ifdef PFH_HAVE_MPFR
#include <mpfr.h>
#endif

namespace detail = pineforge::hpo::detail;

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

#ifdef PFH_HAVE_MPFR
void interval_accuracy() {
    mpfr_t lower_tail, upper_tail, divisor, result;
    mpfr_inits2(256, lower_tail, upper_tail, divisor, result, (mpfr_ptr) nullptr);
    mpfr_sqrt_ui(divisor, 2, MPFR_RNDN);
    double maximum_scaled_error = 0.0;
    for (double center : {-40.0, -8.0, -2.0, -0.1, 0.0, 0.1, 2.0, 8.0, 40.0}) {
        for (double span : {0.24, 0.01, 1e-7}) {
            const double width = span / std::max(1.0, std::abs(center));
            const double lower = center - 0.5 * width;
            const double upper = center + 0.5 * width;
            mpfr_set_d(lower_tail, lower, MPFR_RNDN);
            mpfr_set_d(upper_tail, upper, MPFR_RNDN);
            mpfr_div(lower_tail, lower_tail, divisor, MPFR_RNDN);
            mpfr_div(upper_tail, upper_tail, divisor, MPFR_RNDN);
            if (upper <= 0.0) {
                mpfr_neg(lower_tail, lower_tail, MPFR_RNDN);
                mpfr_neg(upper_tail, upper_tail, MPFR_RNDN);
                mpfr_erfc(lower_tail, lower_tail, MPFR_RNDN);
                mpfr_erfc(upper_tail, upper_tail, MPFR_RNDN);
                mpfr_sub(result, upper_tail, lower_tail, MPFR_RNDN);
            } else {
                mpfr_erfc(lower_tail, lower_tail, MPFR_RNDN);
                mpfr_erfc(upper_tail, upper_tail, MPFR_RNDN);
                mpfr_sub(result, lower_tail, upper_tail, MPFR_RNDN);
            }
            mpfr_div_ui(result, result, 2, MPFR_RNDN);
            mpfr_log(result, result, MPFR_RNDN);
            const double expected = mpfr_get_d(result, MPFR_RNDN);
            const double actual = detail::tpe_log_normal_interval(lower, upper);
            const double scaled_error = std::abs(actual - expected) /
                                        std::max(1.0, std::abs(expected));
            maximum_scaled_error = std::max(maximum_scaled_error, scaled_error);
            require(scaled_error <= 64.0 * std::numeric_limits<double>::epsilon(),
                    "normal-interval series exceeds MPFR error bound");
        }
    }
    mpfr_clears(lower_tail, upper_tail, divisor, result, (mpfr_ptr) nullptr);
    std::cout << "PASS MPFR normal intervals: max scaled log error "
              << maximum_scaled_error << '\n';
}

void accuracy() {
    using Reference = int (*)(mpfr_ptr, mpfr_srcptr, mpfr_rnd_t);
    const std::array<double (*)(double), 6> functions = {detail::math::log,
        detail::math::log1p, detail::math::exp, detail::math::expm1,
        detail::math::cos, detail::math::erfc};
    const std::array<Reference, 6> references = {mpfr_log, mpfr_log1p, mpfr_exp,
                                               mpfr_expm1, mpfr_cos, mpfr_erfc};
    const std::array<double, 22> edges = {0.0, -0.0, 0x1p-1074, -0x1p-1074,
        0x1p-1022, -0x1p-1022, 0x1p-54, -0x1p-54, 0.5, -0.5, 1.0,
        -0x1.fffffffffffffp-1, 0x1.fffffffffffffp-1, 0x1.0000000000001p0,
        0x1.62e42fefa39efp9, -0x1.74910d52d3051p9, 8.0, -8.0, 32.0, -32.0,
        0x1.fffffffffffffp1023, -0x1.fffffffffffffp1023};
    std::mt19937_64 random(170905);
    mpfr_t input, result;
    mpfr_init2(input, 53);
    mpfr_init2(result, 256);
    std::uint64_t checked = 0;
    for (std::uint64_t sample = 0; sample < 20000 + edges.size(); ++sample) {
        const double argument = sample < edges.size() ? edges[sample] :
            pfh_math_value(random() & UINT64_C(0xffefffffffffffff));
        mpfr_set_d(input, argument, MPFR_RNDN);
        for (std::size_t index = 0; index < functions.size(); ++index) {
            if ((index == 0 && argument <= 0.0) || (index == 1 && argument <= -1.0))
                continue;
            mpfr_set_prec(result, 256);
            references[index](result, input, MPFR_RNDN);
            const double expected = mpfr_get_d(result, MPFR_RNDN);
            const double actual = functions[index](argument);
            if (pfh_math_bits(expected) != pfh_math_bits(actual)) {
                std::cerr << "function=" << index << " input=" << std::hexfloat << argument
                          << " expected=" << expected << " actual=" << actual << '\n';
                throw std::runtime_error("portable math differs from MPFR rounding");
            }
            ++checked;
        }
    }
    mpfr_clear(input);
    mpfr_clear(result);
    std::cout << "PASS MPFR-256: " << checked << " binary64 comparisons\n";
}
#endif

int main() {
    try {
        detail::require_portable_environment();
        require(pfh_math_bits(detail::math::log(1.0)) == 0, "log(1)");
        require(pfh_math_bits(detail::math::log1p(-0.0)) == UINT64_C(0x8000000000000000),
                "log1p signed zero");
        require(detail::math::exp(0.0) == 1.0 && detail::math::cos(0.0) == 1.0,
                "exp/cos zero");
        require(detail::math::erfc(0.0) == 1.0, "erfc zero");
        for (double limit : {9.0, 10.0, 20.0, 100.0}) {
            const double tail = detail::math::erfc(limit / 0x1.6a09e667f3bcdp0);
            const double evaluated = detail::math::log(1.0 - tail);
            require(pfh_math_bits(detail::tpe_log_normal_interval(-limit, limit)) ==
                    pfh_math_bits(evaluated), "wide interval exact shortcut");
        }
        require(detail::math::sqrt(0x1p-1074) == 0x1p-537, "subnormal sqrt");
        require(detail::pair_floor_unsigned({0x1p64, -1.0}) == UINT64_MAX,
                "uint64 upper endpoint");
        require(detail::pair_floor_unsigned({0x1p54, -1.0}) == (UINT64_C(1) << 54) - 1,
                "large ordinal residual");
        require(detail::coordinate_ordinal(1.0, UINT64_MAX) == UINT64_MAX - 1,
                "large final ordinal");
        require(detail::coordinate_ordinal(0.5, UINT64_MAX) == (UINT64_C(1) << 63) - 1,
                "large midpoint ordinal");
        require(detail::portable_grid_count(0.0, 0.3, 0.1) == 4, "decimal endpoint");
        require(detail::math::floor(-0.5) == -1.0 && pfh_roundeven(2.5) == 2.0 &&
                pfh_roundeven(-1.5) == -2.0, "rounding helpers");
#ifdef PFH_HAVE_MPFR
        accuracy();
        interval_accuracy();
#endif
        std::cout << "PASS portable math edge contracts\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
