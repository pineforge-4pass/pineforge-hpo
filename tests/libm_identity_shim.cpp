#include <cmath>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>

namespace {

bool selected(const char* function) {
    const char* selection = std::getenv("PFH_SHIM");
    return selection && std::strcmp(selection, function) == 0;
}

template <typename Value>
Value perturb(Value result, const char* function) {
    if (!selected(function))
        return result;
    if (std::getenv("PFH_SHIM_ULP") || std::strstr(function, "fma") ||
        std::strstr(function, "floor") ||
        std::strstr(function, "ceil") || std::strstr(function, "round"))
        return std::nextafter(result, static_cast<Value>(INFINITY));
    return result * (static_cast<Value>(1) + static_cast<Value>(1e-9L));
}

}

#define PFH_WRAP(function, value_type) \
    extern "C" value_type function(value_type argument) noexcept { \
        using Function = value_type (*)(value_type); \
        static const auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, #function)); \
        return perturb(original(argument), #function); \
    }

PFH_WRAP(log, double)
PFH_WRAP(log1p, double)
PFH_WRAP(exp, double)
PFH_WRAP(expm1, double)
PFH_WRAP(sqrt, double)
PFH_WRAP(cos, double)
PFH_WRAP(erfc, double)
PFH_WRAP(floor, double)
PFH_WRAP(ceil, double)
PFH_WRAP(round, double)
PFH_WRAP(logl, long double)
PFH_WRAP(log1pl, long double)
PFH_WRAP(expl, long double)
PFH_WRAP(expm1l, long double)
PFH_WRAP(sqrtl, long double)
PFH_WRAP(cosl, long double)
PFH_WRAP(erfcl, long double)
PFH_WRAP(floorl, long double)
PFH_WRAP(ceill, long double)
PFH_WRAP(roundl, long double)

#undef PFH_WRAP

#define PFH_WRAP_FMA(function, value_type) \
    extern "C" value_type function(value_type first, value_type second, \
                                   value_type third) noexcept { \
        using Function = value_type (*)(value_type, value_type, value_type); \
        static const auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT, #function)); \
        return perturb(original(first, second, third), #function); \
    }

PFH_WRAP_FMA(fma, double)
PFH_WRAP_FMA(fmal, long double)

#undef PFH_WRAP_FMA
