#pragma once

#include <cstdint>
#include <cstring>

extern "C" {
double pfh_cr_log(double value);
double pfh_cr_log1p(double value);
double pfh_cr_exp(double value);
double pfh_cr_expm1(double value);
double pfh_cr_cos(double value);
double pfh_cr_erfc(double value);
}

#include "../../third_party/core_math/portable.h"

namespace pineforge::hpo::detail::math {

inline double log(double value) { return pfh_cr_log(value); }
inline double log1p(double value) { return pfh_cr_log1p(value); }
inline double exp(double value) { return pfh_cr_exp(value); }
inline double expm1(double value) { return pfh_cr_expm1(value); }
inline double cos(double value) { return pfh_cr_cos(value); }
inline double erfc(double value) { return pfh_cr_erfc(value); }
inline double sqrt(double value) { return pfh_sqrt(value); }
inline double fma(double first, double second, double third) {
    return pfh_fma(first, second, third);
}
inline double floor(double value) { return pfh_floor(value); }
inline double ceil(double value) { return -pfh_floor(-value); }
inline double round(double value) { return pfh_round(value); }
inline double nextafter(double value, double toward) { return pfh_nextafter(value, toward); }

}
