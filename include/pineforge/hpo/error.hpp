#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>

namespace pineforge::hpo {

/// Provenance of a failure, independent of its human-readable diagnostic.
enum class FailureOrigin : std::uint8_t { Hpo, Engine };

/// A typed JSON scalar; strings in HPO-owned arguments use closed catalog vocabularies.
class ErrorArgument {
public:
    /// Supported scalar types, including exact signed and unsigned persistence integers.
    using Value =
        std::variant<std::nullptr_t, bool, std::int64_t, std::uint64_t, double, std::string>;
    /// Constructs a null argument.
    ErrorArgument(std::nullptr_t value) : value_(value) {}
    /// Constructs a Boolean argument.
    ErrorArgument(bool value) : value_(value) {}
    /// Constructs a signed integer argument.
    ErrorArgument(std::int64_t value) : value_(value) {}
    /// Constructs an unsigned integer argument.
    ErrorArgument(std::uint64_t value) : value_(value) {}
    /// Constructs a numeric argument.
    ErrorArgument(double value) : value_(value) {}
    /// Constructs a string argument without converting string literals to Boolean.
    ErrorArgument(const char* value) : value_(std::string(value)) {}
    /// Constructs a string argument.
    ErrorArgument(std::string value) : value_(std::move(value)) {}
    /// Returns the scalar value.
    const Value& value() const noexcept { return value_; }

private:
    Value value_;
};

/// Deterministically ordered failure arguments. No strategy-computed value belongs here.
using ErrorArguments = std::map<std::string, ErrorArgument>;

/// Stable machine-readable failure metadata shared by the standard exception families.
/// Catch HpoError for metadata, or the original standard family for legacy compatibility.
class HpoError {
public:
    /// Constructs metadata; HPO codes and argument vocabularies are defined by the catalog.
    HpoError(std::string code, ErrorArguments args = {}, FailureOrigin origin = FailureOrigin::Hpo)
        : code_(std::move(code)), args_(std::move(args)), origin_(origin) {}
    virtual ~HpoError() = default;
    /// Returns the unchanged legacy English diagnostic.
    virtual const char* what() const noexcept = 0;
    /// Returns the stable failure code, never inferred from diagnostic text.
    const std::string& code() const noexcept { return code_; }
    /// Returns typed, catalog-declared scalar arguments.
    const ErrorArguments& args() const noexcept { return args_; }
    /// Returns the component which owns the code.
    FailureOrigin origin() const noexcept { return origin_; }

private:
    std::string code_;
    ErrorArguments args_;
    FailureOrigin origin_;
};

/// Adds stable metadata while preserving a standard exception's catch compatibility.
template <typename Base = std::runtime_error>
class TypedHpoError : public Base, public HpoError {
public:
    /// Constructs a coded exception with unchanged English text and optional provenance.
    TypedHpoError(std::string code,
                  ErrorArguments args,
                  const std::string& message,
                  FailureOrigin origin = FailureOrigin::Hpo)
        : Base(message), HpoError(std::move(code), std::move(args), origin) {}
    /// Returns the original standard exception diagnostic.
    const char* what() const noexcept override { return Base::what(); }
};

/// An engine-owned failure whose optional metadata is forwarded without reclassification.
class EngineError : public TypedHpoError<> {
public:
    /// Preserves unavailable getters as null metadata and the original diagnostic text.
    EngineError(std::optional<std::string> code,
                std::optional<std::string> args,
                const std::string& message)
        : TypedHpoError<>(code.value_or(""), {}, message, FailureOrigin::Engine),
          raw_args_(std::move(args)) {}
    /// Returns the untrusted engine JSON for transport validation, or unavailable metadata.
    const std::optional<std::string>& raw_args() const noexcept { return raw_args_; }

private:
    std::optional<std::string> raw_args_;
};

}  // namespace pineforge::hpo
