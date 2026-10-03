#include "../../src/core/ordinal_set.hpp"

#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <sys/resource.h>

int main(int argc, char** argv) {
    if (argc != 3)
        throw std::invalid_argument("usage: ordinal-profile dense|disk count");
    const std::string mode = argv[1];
    const auto count = std::stoull(argv[2]);
    pineforge::hpo::detail::OrdinalSet ordinals(mode == "dense" ? 100000000 : 0);
    const auto started = std::chrono::steady_clock::now();
    for (std::uint64_t index = 0; index < count; ++index) {
        if (!ordinals.insert(index * 1000003 % 100000000))
            throw std::runtime_error("ordinal probe unexpectedly repeated a value");
    }
    const auto seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - started).count();
    struct rusage usage {};
    getrusage(RUSAGE_SELF, &usage);
    std::cout << "{\"mode\":\"" << mode << "\",\"count\":" << count
              << ",\"seconds\":" << seconds << ",\"insert_us\":" << seconds * 1e6 / count
              << ",\"peak_rss_kib\":" << usage.ru_maxrss << "}\n";
}
