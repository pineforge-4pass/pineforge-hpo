// Both public return-statistics headers in one translation unit, and the real build's generated
// identity read back against its own descriptor.
//
// UNEXECUTED until the proof phase. The identity header is included first here; main.cpp and the
// executor include the reducer header first, so both orders are compiled by the product. If either
// header defined a name of the other again (the old duplicate `kReturnStatsContract`), this file
// would not compile.
//
// The generated header exists only after the build step: this target depends on the generation
// target and takes its include directory from it (tests/CMakeLists.txt).
//
// Exit codes: 0 pass; 1 failure. With PFH_REQUIRE_RETURN_STATS_IDENTITY=1 (the proof
// configuration) an unbound identity is a failure; otherwise an unbound build is reported and its
// recorded facts are checked, because an unbound build is a supported, runnable configuration.

#include <pineforge/hpo/return_stats_identity.hpp>
#include <pineforge/hpo/return_stats.hpp>

#include "../src/core/sha256.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace pfh = pineforge::hpo;

// The contract bound at build time and the reducer's constant are separate; they must agree.
static_assert(pfh::return_stats_contract() == pfh::kReturnStatsContract,
              "identity and reducer disagree on the contract");
static_assert(pfh::kReturnStatsRiskFreeAnnual == 0.02, "fixed annual risk-free rate");
static_assert(pfh::kReturnStatsMetricCount == 18, "exactly eighteen metric names");

namespace {

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

bool lower_hex(std::string_view text, std::size_t length) {
    if (text.size() != length)
        return false;
    for (const char c : text)
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
            return false;
    return true;
}

bool contains(std::string_view text, std::string_view part) {
    return text.find(part) != std::string_view::npos;
}

}  // namespace

int main() {
    try {
        const std::string_view contract = pfh::return_stats_contract();
        const std::string_view identity = pfh::return_stats_numeric_build_identity();
        const std::string_view descriptor = pfh::return_stats_identity_descriptor();
        const std::string_view source_digest = pfh::return_stats_source_digest();
        const std::string_view reason = pfh::return_stats_identity_unbound_reason();
        const bool required = std::getenv("PFH_REQUIRE_RETURN_STATS_IDENTITY") != nullptr &&
            std::string(std::getenv("PFH_REQUIRE_RETURN_STATS_IDENTITY")) == "1";

        require(contract == "pineforge-hpo-return-stats/v1", "unexpected bound contract");
        require(descriptor.rfind("pineforge-hpo-return-stats-identity/v2\n", 0) == 0,
                "descriptor does not start with the v2 format line");
        require(contains(descriptor, "contract=pineforge-hpo-return-stats/v1\n"),
                "descriptor lacks the contract line");

        if (pfh::return_stats_identity_bound()) {
            constexpr std::string_view prefix = "pineforge-hpo-return-stats-build/v2:sha256:";
            require(reason.empty(), "a bound build has an unbound reason");
            require(identity.rfind(prefix, 0) == 0, "identity prefix is not v2");
            require(lower_hex(identity.substr(prefix.size()), 64), "identity is not 64 hex digits");
            require(std::string(identity.substr(prefix.size())) ==
                        pineforge::hpo::detail::sha256(descriptor),
                    "identity is not the SHA-256 of the retained descriptor");
            require(lower_hex(source_digest, 64), "source digest is not 64 hex digits");
            require(contains(descriptor, "\nsource.digest=" + std::string(source_digest) + "\n"),
                    "descriptor does not carry the source digest");
            require(!contains(descriptor, "capability=unbound"), "bound descriptor says unbound");
            require(contains(descriptor, "\ncompiler.sha256="), "no compiler custody line");
            require(contains(descriptor, "\ncommand <src>/src/core/return_stats.cpp\n"),
                    "no command block for the reducer source");
            require(contains(descriptor, "\nsource <src>/src/core/return_stats.cpp sha256="),
                    "reducer source not listed with its digest");
            require(contains(descriptor,
                             "\nheader <src>/include/pineforge/hpo/return_stats.hpp sha256="),
                    "reducer header not listed with its digest");
            // The per-source options of the real core reach the generated command.
            for (const char* flag : {"-ffp-contract=off", "-fno-fast-math", "-frounding-math",
                                     "-fno-builtin", "-fno-lto"})
                require(contains(descriptor, std::string("\narg ") + flag + "\n"),
                        std::string("generated reducer command lacks ") + flag);
            // The executed command must be compared with this descriptor by the integration
            // test: the compilation database is generator intent, not proof of the invocation.
            std::cout << "BOUND " << identity << '\n';
        } else {
            require(!reason.empty(), "an unbound build has no reason");
            require(identity.empty() && source_digest.empty(),
                    "an unbound build carries an identity or a source digest");
            require(std::string(descriptor) ==
                        "pineforge-hpo-return-stats-identity/v2\ncontract=" +
                            std::string(contract) + "\ncapability=unbound\nreason=" +
                            std::string(reason) + "\n",
                    "unbound descriptor differs from the documented short form");
            std::cout << "UNBOUND " << reason << '\n';
            if (required)
                throw std::runtime_error("the identity is required to be bound (" +
                                         std::string(reason) + ")");
        }
        std::cout << "both return-statistics headers and the generated identity are consistent\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
