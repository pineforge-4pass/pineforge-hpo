#include "../src/cli/fd6_emission.hpp"

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <variant>

namespace {

using pineforge::hpo::ErrorArgument;
using pineforge::hpo::FailureOrigin;
using pineforge::hpo::HpoError;
using pineforge::hpo::cli::fd6::Fd6EmissionProjector;

void write_string(const std::string& value) {
    constexpr char hex[] = "0123456789abcdef";
    std::cout << '"';
    for (const unsigned char byte : value) {
        switch (byte) {
        case '"':
            std::cout << "\\\"";
            break;
        case '\\':
            std::cout << "\\\\";
            break;
        default:
            if (byte < 0x20)
                std::cout << "\\u00" << hex[byte >> 4] << hex[byte & 0x0f];
            else
                std::cout << static_cast<char>(byte);
            break;
        }
    }
    std::cout << '"';
}

// Serialize the variant actually returned by HpoError, including signedness.
void write_argument(const ErrorArgument& argument) {
    std::visit(
        [](const auto& value) {
            using T = std::decay_t<decltype(value)>;
            if constexpr (std::is_same_v<T, std::nullptr_t>) {
                std::cout << "{\"type\":\"null\",\"value\":null}";
            } else if constexpr (std::is_same_v<T, bool>) {
                std::cout << "{\"type\":\"boolean\",\"value\":"
                          << (value ? "true" : "false") << '}';
            } else if constexpr (std::is_same_v<T, std::int64_t>) {
                std::cout << "{\"type\":\"int64\",\"value\":" << value << '}';
            } else if constexpr (std::is_same_v<T, std::uint64_t>) {
                std::cout << "{\"type\":\"uint64\",\"value\":" << value << '}';
            } else if constexpr (std::is_same_v<T, double>) {
                if (!std::isfinite(value))
                    throw std::runtime_error("non-finite error argument");
                std::cout << "{\"type\":\"number\",\"value\":"
                          << std::setprecision(std::numeric_limits<double>::max_digits10)
                          << value << '}';
            } else if constexpr (std::is_same_v<T, std::string>) {
                std::cout << "{\"type\":\"string\",\"value\":";
                write_string(value);
                std::cout << '}';
            }
        },
        argument.value());
}

class Observations {
public:
    template <typename Operation>
    void capture(const char* name,
                 std::optional<std::uint64_t> input_id,
                 Operation&& operation) {
        // These catches exercise the original standard families. The cross-cast
        // below obtains metadata from the same real exception, never a new error.
        try {
            operation();
        } catch (const std::invalid_argument& error) {
            write(name, input_id, error, "std::invalid_argument");
            return;
        } catch (const std::logic_error& error) {
            write(name, input_id, error, "std::logic_error");
            return;
        } catch (const std::overflow_error& error) {
            write(name, input_id, error, "std::overflow_error");
            return;
        } catch (const std::exception& error) {
            write(name, input_id, error, "std::exception");
            return;
        } catch (...) {
            throw std::runtime_error(std::string(name) + ": non-standard exception");
        }
        throw std::runtime_error(std::string(name) + ": projector accepted refused operation");
    }

private:
    void write(const char* name,
               std::optional<std::uint64_t> input_id,
               const std::exception& standard_error,
               const char* standard_family) {
        const auto* error = dynamic_cast<const HpoError*>(&standard_error);
        if (!error)
            throw std::runtime_error(std::string(name) + ": missing HpoError metadata");

        if (!first_)
            std::cout << ",\n";
        first_ = false;
        std::cout << "{\"case\":";
        write_string(name);
        std::cout << ",\"input_id\":";
        if (input_id)
            std::cout << *input_id;
        else
            std::cout << "null";
        std::cout << ",\"code\":";
        write_string(error->code());
        std::cout << ",\"origin\":";
        switch (error->origin()) {
        case FailureOrigin::Hpo:
            write_string("hpo");
            break;
        case FailureOrigin::Engine:
            write_string("engine");
            break;
        default:
            throw std::runtime_error(std::string(name) + ": unknown failure origin");
        }
        std::cout << ",\"args\":{";
        bool first_argument = true;
        for (const auto& [key, argument] : error->args()) {
            if (!first_argument)
                std::cout << ',';
            first_argument = false;
            write_string(key);
            std::cout << ':';
            write_argument(argument);
        }
        std::cout << "},\"what\":";
        write_string(error->what());
        std::cout << ",\"standard_family\":";
        write_string(standard_family);
        std::cout << ",\"standard_what\":";
        write_string(standard_error.what());
        std::cout << '}';
    }

    bool first_ = true;
};

void observe_id_paths(Observations& observations, std::uint64_t id) {
    Fd6EmissionProjector finalized(id);
    finalized.flush(true);
    observations.capture("finalized_issue", id, [&] { finalized.issue(id); });
    observations.capture("finalized_observe", id,
                         [&] { finalized.observe({id, "ok", true}); });
    observations.capture("finalized_skip", id, [&] { finalized.skip(id); });

    // UINT64_MAX cannot have a greater floor; use its predecessor for this case.
    const auto below = id == std::numeric_limits<std::uint64_t>::max() ? id - 1 : id;
    Fd6EmissionProjector floor(below + 1);
    observations.capture("below_floor", below, [&] { floor.issue(below); });

    Fd6EmissionProjector issued(id);
    issued.issue(id);
    observations.capture("duplicate_issue", id, [&] { issued.issue(id); });
    observations.capture("unknown_status", id,
                         [&] { issued.observe({id, "unrecognized_fd6_status", false}); });

    Fd6EmissionProjector unissued(id);
    observations.capture("unissued_observe", id,
                         [&] { unissued.observe({id, "ok", true}); });
    observations.capture("unissued_skip", id, [&] { unissued.skip(id); });

    Fd6EmissionProjector terminal(id);
    terminal.issue(id);
    terminal.observe({id, "ok", true});
    observations.capture("pending_after_terminal", id,
                         [&] { terminal.observe({id, "pending", false}); });
    observations.capture("duplicate_terminal", id,
                         [&] { terminal.observe({id, "engine_error", false}); });
    observations.capture("skip_after_terminal", id, [&] { terminal.skip(id); });

    Fd6EmissionProjector skipped(id);
    skipped.issue(id);
    skipped.skip(id);
    observations.capture("pending_after_skip", id,
                         [&] { skipped.observe({id, "pending", false}); });
    observations.capture("duplicate_skip", id, [&] { skipped.skip(id); });
}

}  // namespace

int main(int argc, char**) {
    if (argc != 1) {
        std::cerr << "usage: fd6-catalog-subject\n";
        return 2;
    }
    try {
        std::cout << "{\"schema\":\"pineforge-hpo-fd6-observations/v1\","
                     "\"observations\":[\n";
        Observations observations;
        constexpr std::array<std::uint64_t, 4> ids{
            0, 17, (std::uint64_t{1} << 63) + 17,
            std::numeric_limits<std::uint64_t>::max()};
        for (const auto id : ids)
            observe_id_paths(observations, id);

        Fd6EmissionProjector finalized;
        finalized.flush(true);
        observations.capture("finalized_flush", std::nullopt,
                             [&] { finalized.flush(false); });
        std::cout << "\n]}\n";
        std::cout.flush();
        if (!std::cout)
            throw std::runtime_error("cannot write FD6 observations");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "fd6-catalog-subject: " << error.what() << '\n';
        return 2;
    } catch (...) {
        std::cerr << "fd6-catalog-subject: non-standard setup failure\n";
        return 2;
    }
}
