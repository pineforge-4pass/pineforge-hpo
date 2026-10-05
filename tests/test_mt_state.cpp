#include "../src/core/mt19937_64.hpp"
#include "../src/core/dimension_workers.hpp"
#include "../src/core/sha256.hpp"

#include <cstdlib>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <system_error>

namespace detail = pineforge::hpo::detail;

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

int main() {
    try {
        std::ifstream input(PFH_MT_FIXTURE);
        require(input.good(), "canonical MT golden fixture missing");
        std::ostringstream transcript;
        for (const auto seed : {17ULL, 0x9e3779b97f4a7c15ULL}) {
            detail::Mt19937_64 restored;
            restored.read(input);
            std::mt19937_64 native(seed);
            native.discard(seed == 17 ? 999 : 201);
            for (std::size_t index = 0; index < 4096; ++index) {
                const auto actual = restored();
                require(actual == native(), "canonical MT differs from native MT golden stream");
                transcript << actual << ' ';
            }
        }
        require(detail::sha256(transcript.str()) == PFH_MT_GOLDEN_HASH,
                "cross-stdlib MT golden hash differs");
        for (const auto seed : std::array<std::uint64_t, 4>{0, 1, 5489, UINT64_MAX}) {
            detail::Mt19937_64 canonical(seed);
            std::mt19937_64 native(seed);
            for (std::size_t index = 0; index < 10000; ++index)
                require(canonical() == native(), "MT seed/twist/tempering differs");
        }
        std::ostringstream zero;
        zero << "MT64 312 ";
        for (std::size_t index = 0; index < 312; ++index)
            zero << "0 ";
        zero << "312";
        std::istringstream malformed(zero.str());
        bool rejected = false;
        try {
            detail::Mt19937_64 engine;
            engine.read(malformed);
        } catch (const std::invalid_argument&) {
            rejected = true;
        }
        require(rejected, "all-zero MT state accepted");
        require(detail::quota_cpus("max", 100000) == 0 &&
                detail::quota_cpus("-1", 100000) == 0 &&
                detail::quota_cpus("200000", 100000) == 2 &&
                detail::quota_cpus("150000", 100000) == 1 &&
                detail::quota_cpus("50000", 100000) == 1,
                "cgroup v1/v2 quota calculation differs");
        if (const auto* limit = std::getenv("PFH_EXPECT_CPU_LIMIT"))
            require(detail::available_cpus() == std::stoul(limit), "cgroup CPU limit ignored");
        unsigned created = 0;
        detail::DimensionWorkers workers([&](std::function<void()> function) {
            ++created;
            return std::thread(std::move(function));
        });
        std::vector<unsigned> values(32);
        for (std::size_t repeat = 0; repeat < 24; ++repeat)
            workers.run(values.size(), 8, [&](std::size_t index) { ++values[index]; });
        require(created == 7, "EI attempts recreated workers");
        require(std::all_of(values.begin(), values.end(), [](auto value) { return value == 24; }),
                "persistent workers skipped/repeated dimensions");
        unsigned attempts = 0;
        detail::DimensionWorkers fallback([&](std::function<void()> function) {
            if (++attempts == 3)
                throw std::system_error(
                    std::make_error_code(std::errc::resource_unavailable_try_again));
            return std::thread(std::move(function));
        });
        for (std::size_t repeat = 0; repeat < 2; ++repeat)
            fallback.run(values.size(), 8, [&](std::size_t index) { ++values[index]; });
        require(attempts == 3 && std::all_of(values.begin(), values.end(),
                [](auto value) { return value == 26; }), "thread failure did not fall back serial");
        std::cout << "PASS canonical MT golden/native streams, zero state, quotas, "
                     "reuse/fallback\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
