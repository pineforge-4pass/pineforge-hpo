// Runtime checks of the Sobol numeric identity (include/pineforge/hpo/sobol_identity.hpp).
//
// These checks read the identity of the build they are linked into. They prove the composition:
// the digest field equals the SHA-256 of the retained descriptor, every bound command carries the
// strict arithmetic options, the evidence fields have their shape, the identity shares nothing
// with the TPE identity, and an unbound build refuses with the typed error after the portable
// environment check. They do not prove arithmetic, repeat or worker-count invariance, and they say
// nothing about another architecture. With PFH_REQUIRE_SOBOL_IDENTITY set, an unbound build fails.
#include <pineforge/hpo/error.hpp>
#include <pineforge/hpo/sobol_identity.hpp>

#include "../src/core/sha256.hpp"
#include "../src/core/sobol_engine.hpp"
#include "../src/core/sobol_mapper.hpp"

#if defined(__x86_64__)
#include <xmmintrin.h>
#endif

#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace {

using pineforge::hpo::HpoError;

int failures = 0;

void check(bool condition, const std::string& what) {
    if (!condition) {
        std::cerr << "FAIL: " << what << "\n";
        ++failures;
    }
}

std::string field(const std::string& identity, const std::string& name) {
    const std::string marker = ";" + name + ":";
    const auto begin = identity.find(marker);
    if (begin == std::string::npos)
        return {};
    const auto start = begin + marker.size();
    const auto end = identity.find(';', start);
    return identity.substr(start, end == std::string::npos ? std::string::npos : end - start);
}

bool is_hex64(const std::string& value) {
    if (value.size() != 64)
        return false;
    for (const char character : value)
        if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f')))
            return false;
    return true;
}

// Descriptor split at "command " lines: one block per bound translation unit.
std::vector<std::string> command_blocks(const std::string& descriptor) {
    std::vector<std::string> blocks;
    std::istringstream input(descriptor);
    std::string line;
    while (std::getline(input, line)) {
        if (line.rfind("command ", 0) == 0)
            blocks.push_back(line + "\n");
        else if (line.rfind("arg ", 0) == 0 && !blocks.empty())
            blocks.back() += line + "\n";
    }
    return blocks;
}

struct Refusal {
    bool thrown = false;
    std::string code;
    std::string reason;
    std::string text;
};

Refusal attempt() {
    Refusal refusal;
    try {
        (void)pineforge::hpo::sobol_numeric_build_identity();
    } catch (const HpoError& error) {
        refusal.thrown = true;
        refusal.code = error.code();
        const auto found = error.args().find("reason");
        if (found != error.args().end())
            refusal.reason = std::get<std::string>(found->second.value());
        refusal.text = error.what();
    } catch (const std::exception& error) {
        refusal.thrown = true;
        refusal.text = error.what();
    }
    return refusal;
}

void consistency() {
    const bool bound = pineforge::hpo::sobol_numeric_identity_bound();
    const std::string reason(pineforge::hpo::sobol_numeric_identity_unbound_reason());
    check(bound == reason.empty(), "bound exactly when the unbound reason is empty");
    const std::string descriptor(pineforge::hpo::sobol_numeric_identity_descriptor());
    check(!descriptor.empty(), "the descriptor is retained in every build");
    if (!bound) {
        check(descriptor.find("capability=unbound") != std::string::npos &&
                  descriptor.find("reason=" + reason) != std::string::npos,
              "an unbound descriptor names its reason");
        return;
    }
    check(descriptor.rfind("pineforge-hpo-sobol-identity/v1\n", 0) == 0, "descriptor header");
    check(descriptor.find("contract=portable-sobol-v1\n") != std::string::npos, "contract line");
    check(descriptor.find("compiler.cxx.sha256=") != std::string::npos &&
              descriptor.find("compiler.c.sha256=") != std::string::npos,
          "both compiler drivers are bound by their bytes");
    check(descriptor.find("capability=unbound") == std::string::npos, "bound descriptor is full");

    const auto blocks = command_blocks(descriptor);
    int cxx = 0;
    int c = 0;
    int provider = 0;
    for (const auto& block : blocks) {
        if (block.rfind("command cxx ", 0) == 0)
            ++cxx;
        else if (block.rfind("command provider ", 0) == 0)
            ++provider;
        else if (block.rfind("command c ", 0) == 0)
            ++c;
        check(block.find("arg -ffp-contract=off\n") != std::string::npos,
              "no-contraction option in the bound command: " + block.substr(0, block.find('\n')));
        check(block.find("arg -fno-fast-math\n") != std::string::npos,
              "no-fast-math option in the bound command: " + block.substr(0, block.find('\n')));
        check(block.find("arg -ffast-math\n") == std::string::npos, "no fast-math token");
    }
    check(cxx == 4, "four bound C++ translation units, got " + std::to_string(cxx));
    check(c >= 7, "the whole portable-math target is bound, got " + std::to_string(c));
    // Conservative provider coupling: every shipped core unit that can emit the linker's copy of a
    // shared inline helper is bound by command and source digest, strict flags or not (the loop
    // above requires the strict recipe of all of today's providers, so a new provider without it
    // is noticed here).
    check(provider == 2, "two bound shared-helper providers, got " + std::to_string(provider));
    for (const char* name : {"search_space.cpp", "tpe_sampler.cpp"}) {
        check(descriptor.find(std::string("command provider <src>/src/core/") + name + "\n") !=
                  std::string::npos,
              std::string("bound provider command ") + name);
        check(descriptor.find(std::string("\nsource <src>/src/core/") + name + " sha256=") !=
                  std::string::npos,
              std::string("bound provider source digest ") + name);
    }
    for (const char* name : {"sobol_engine.cpp", "sobol_mapper.cpp", "sobol_sampler.cpp",
                             "sobol_identity.cpp"})
        check(descriptor.find(std::string("command cxx <src>/src/core/") + name + "\n") !=
                  std::string::npos,
              std::string("bound C++ unit ") + name);
    for (const char* name : {"portable_math_canary.c", "log.c", "log1p.c", "exp.c", "expm1.c"})
        check(descriptor.find(std::string("/") + name + "\n") != std::string::npos,
              std::string("bound C unit ") + name);
    check(descriptor.find("sobol_table_joe_kuo_d6_1024.inc sha256=") != std::string::npos,
          "the generated table is bound by content");
    check(descriptor.find("sobol_identity_generated") == std::string::npos,
          "the generated header is not an input of its own digest");
}

void identity_shape() {
    if (!pineforge::hpo::sobol_numeric_identity_bound())
        return;
    const std::string first = pineforge::hpo::sobol_numeric_build_identity();
    const std::string second = pineforge::hpo::sobol_numeric_build_identity();
    check(first == second, "the identity is deterministic within a process");
    check(first.rfind("portable-sobol-v1;", 0) == 0, "prefix portable-sobol-v1");
    namespace detail = pineforge::hpo::detail;
    check(field(first, "mapper") ==
              std::string(detail::kSobolMapperContract) + ":r" +
                  std::to_string(detail::kSobolMapperRevision),
          "mapper contract and revision");
    check(field(first, "table") ==
              std::string(detail::kSobolTableName) + ":" + detail::kSobolTableSubsetSha256 + ":w" +
                  std::to_string(detail::kSobolWordBits),
          "table name, subset digest and word size");
    const std::string descriptor(pineforge::hpo::sobol_numeric_identity_descriptor());
    check(field(first, "build_sha256") == detail::sha256(descriptor),
          "build_sha256 is the SHA-256 of the retained descriptor");
    check(is_hex64(field(first, "portable_probe_sha256")), "portable probe digest shape");
    check(field(first, "binary64") == "53" && field(first, "eval") == "0" &&
              field(first, "round") == "nearest" && field(first, "subnormals") == "gradual",
          "arithmetic environment fields");

    // Canaries: strictly compiled C++ and C translation units do not contract, so the unfused
    // value differs from the fused one, and the C value equals the unfused C++ value.
    const std::string canary = field(first, "contract_canary");
    const auto colon = canary.find(':');
    check(colon != std::string::npos, "contract canary has two values");
    if (colon != std::string::npos) {
        const std::string arithmetic = canary.substr(0, colon);
        const std::string fused = canary.substr(colon + 1);
        check(arithmetic != fused, "the C++ translation unit did not contract");
        check(field(first, "c_canary") == arithmetic,
              "the C canary equals the unfused C++ value");
    }

    // Independence: nothing of the TPE or return-statistics identities.
    for (const char* marker : {"portable-tpe", "libm_functions", "semantic_flags", "core_math:",
                               "flags_sha256", "return-stats"})
        check(first.find(marker) == std::string::npos,
              std::string("the identity contains the foreign marker ") + marker);
}

void unbound_refusal() {
    if (pineforge::hpo::sobol_numeric_identity_bound()) {
        check(!attempt().thrown, "a bound build returns an identity");
        return;
    }
    if (std::getenv("PFH_REQUIRE_SOBOL_IDENTITY") != nullptr)
        check(false, "the Sobol identity is unbound but a bound build is required: " +
                         std::string(pineforge::hpo::sobol_numeric_identity_unbound_reason()));
    const Refusal refusal = attempt();
    check(refusal.thrown, "an unbound build refuses to produce an identity");
    check(refusal.code == "hpo_toolchain_unavailable" && refusal.reason == "native_runner",
          "typed refusal hpo_toolchain_unavailable/native_runner, got " + refusal.code + "/" +
              refusal.reason);
    check(refusal.text.find(std::string(
              pineforge::hpo::sobol_numeric_identity_unbound_reason())) != std::string::npos,
          "the refusal names the exact unbound reason");
}

// The portable environment is checked before the unbound refusal and before any evidence.
void environment_first() {
#if defined(__x86_64__)
    const unsigned saved = _mm_getcsr();
    _mm_setcsr(saved | 0x8000U);  // flush-to-zero on
    Refusal refusal;
    try {
        refusal = attempt();
    } catch (...) {
        _mm_setcsr(saved);
        throw;
    }
    _mm_setcsr(saved);
    check(refusal.thrown && refusal.code == "hpo_portable_math_unavailable",
          "flush-to-zero is refused first with hpo_portable_math_unavailable, got " +
              refusal.code);
#else
    std::cout << "SKIP environment_first: no portable way to change the rounding state here\n";
#endif
}

}  // namespace

int main() {
    consistency();
    identity_shape();
    unbound_refusal();
    environment_first();
    if (failures != 0) {
        std::cerr << failures << " check(s) failed\n";
        return 1;
    }
    std::cout << "PASS: sobol numeric identity ("
              << (pineforge::hpo::sobol_numeric_identity_bound() ? "bound" : "unbound") << ")\n";
    return 0;
}
