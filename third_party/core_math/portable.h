#ifndef PFH_CORE_MATH_PORTABLE_H
#define PFH_CORE_MATH_PORTABLE_H

#include <stdint.h>
#include <string.h>

static inline uint64_t pfh_math_bits(double value) {
    uint64_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return bits;
}

static inline double pfh_math_value(uint64_t bits) {
    double value;
    memcpy(&value, &bits, sizeof(value));
    return value;
}

static inline double pfh_fma(double first, double second, double third) {
#if defined(__aarch64__)
    double result;
    __asm__("fmadd %d0, %d1, %d2, %d3" : "=w"(result)
            : "w"(first), "w"(second), "w"(third));
    return result;
#elif defined(__x86_64__)
    __asm__("vfmadd213sd %2, %1, %0" : "+x"(first) : "x"(second), "x"(third));
    return first;
#else
#error "Portable TPE requires aarch64 or x86-64 with FMA3"
#endif
}

static inline double pfh_sqrt(double value) {
#if defined(__aarch64__)
    double result;
    __asm__("fsqrt %d0, %d1" : "=w"(result) : "w"(value));
    return result;
#elif defined(__x86_64__)
    __asm__("sqrtsd %0, %0" : "+x"(value));
    return value;
#endif
}

static inline double pfh_floor(double value) {
    const uint64_t bits = pfh_math_bits(value);
    const int exponent = (int)((bits >> 52) & 0x7ff) - 1023;
    if (exponent >= 52 || !(bits << 1))
        return value;
    if (exponent < 0)
        return bits >> 63 ? -1.0 : 0.0;
    const uint64_t mask = (UINT64_C(1) << (52 - exponent)) - 1;
    const double integral = pfh_math_value(bits & ~mask);
    return (bits >> 63) && (bits & mask) ? integral - 1.0 : integral;
}

static inline double pfh_roundeven(double value) {
    const double lower = pfh_floor(value);
    const double fraction = value - lower;
    if (fraction < 0.5)
        return lower;
    if (fraction > 0.5)
        return lower + 1.0;
    if (fraction == 0.5) {
        const double half = pfh_floor(lower * 0.5);
        const double result = lower == 2.0 * half ? lower : lower + 1.0;
        return result == 0.0 ? pfh_math_value(pfh_math_bits(value) & (UINT64_C(1) << 63)) :
               result;
    }
    return value;
}

static inline double pfh_round(double value) {
    const int negative = (int)(pfh_math_bits(value) >> 63);
    const double magnitude = negative ? -value : value;
    const double lower = pfh_floor(magnitude);
    const double integral = magnitude - lower >= 0.5 ? lower + 1.0 : lower;
    return negative ? -integral : integral;
}

static inline double pfh_nextafter(double value, double toward) {
    if (value == toward)
        return toward;
    uint64_t bits = pfh_math_bits(value);
    if (!(bits << 1))
        return pfh_math_value((pfh_math_bits(toward) & (UINT64_C(1) << 63)) | 1);
    if ((value < toward) == (value > 0.0))
        ++bits;
    else
        --bits;
    return pfh_math_value(bits);
}

static inline double pfh_ldexp(double value, int exponent) {
    if (exponent > 1023) {
        value *= 0x1p1023;
        exponent -= 1023;
        if (exponent > 1023) {
            value *= 0x1p1023;
            exponent -= 1023;
            if (exponent > 1023)
                exponent = 1023;
        }
    } else if (exponent < -1022) {
        value *= 0x1p-969;
        exponent += 969;
        if (exponent < -1022) {
            value *= 0x1p-969;
            exponent += 969;
            if (exponent < -1022)
                exponent = -1022;
        }
    }
    return value * pfh_math_value((uint64_t)(exponent + 1023) << 52);
}

#ifndef __cplusplus
#define cr_log pfh_cr_log
#define cr_log1p pfh_cr_log1p
#define cr_exp pfh_cr_exp
#define cr_expm1 pfh_cr_expm1
#define cr_cos pfh_cr_cos
#define cr_erfc pfh_cr_erfc
#define __builtin_fma pfh_fma
#define __builtin_roundeven pfh_roundeven
#define __builtin_round pfh_round
#define __builtin_floor pfh_floor
#define __builtin_ldexp pfh_ldexp
#endif

#endif
