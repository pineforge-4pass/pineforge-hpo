#pragma once

#include <string>
#include <string_view>

namespace pineforge::hpo {

/// True when this build's Sobol numeric identity is bound to the compile commands that the build
/// system generated for the Sobol sources and the portable-math target.
///
/// It is false for builds that cannot be bound exactly: multi-configuration generators, compiler
/// launchers or wrappers, response files, a disabled, missing or ambiguous compilation database
/// entry, or a CMake older than 3.19. Such a build compiles and runs as usual, including Sobol
/// spaces without a floating-point column; only the floating-point identity is unavailable.
bool sobol_numeric_identity_bound();

/// Short code naming why the identity is unbound; empty when it is bound.
std::string_view sobol_numeric_identity_unbound_reason();

/// Numeric build identity of the floating-point Sobol mapper, starting with `portable-sobol-v1`.
///
/// Request it only for a space with a varying real, stepped-real or log-integer column; a
/// discrete-only space has no numeric identity. The portable-math environment is checked first and
/// refused with the same typed error as the TPE sampler. An unbound build then throws
/// `hpo_toolchain_unavailable` (reason `native_runner`) naming the exact unbound reason, so a
/// missing identity can neither be published nor admit a floating-point continuation.
///
/// The string binds the mapper contract and revision, the Joe-Kuo table name, subset digest and
/// word size, the digest of the build descriptor (sources, generated table, compile commands of
/// every Sobol and portable-math translation unit, both compiler drivers), and the current
/// rounding, gradual-underflow, contraction-canary and portable-probe evidence. The evidence is a
/// sanity limit of this process, not an attestation: equal strings name equal builds, they do not
/// show that two builds or two architectures compute equal values, and they are independent of the
/// TPE checkpoint identity and of the return-statistics identity.
std::string sobol_numeric_build_identity();

/// Exact descriptor text whose SHA-256 digest is the `build_sha256` field of the identity.
///
/// Proof tooling compares it with the compilation database of the build; it is not part of any
/// result. An unbound build carries a short descriptor that names the reason.
std::string_view sobol_numeric_identity_descriptor();

}  // namespace pineforge::hpo
