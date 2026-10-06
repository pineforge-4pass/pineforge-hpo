#if defined(__x86_64__)
__attribute__((target("fma")))
#endif
double pfh_math_contraction_canary(double first, double second, double third) {
    return first * second + third;
}
