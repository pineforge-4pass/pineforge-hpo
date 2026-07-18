#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>

#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <variant>

namespace {

template <typename Value>
const Value& parameter(const pineforge::hpo::Candidate& candidate,
                       const std::string& name) {
    const auto* value = candidate.find(name);
    if (value == nullptr || !std::holds_alternative<Value>(*value)) {
        throw std::runtime_error("candidate parameter has an unexpected type: " + name);
    }
    return std::get<Value>(*value);
}

double evaluate(const pineforge::hpo::Candidate& candidate) {
    const auto length = parameter<std::int64_t>(candidate, "length");
    const double factor = parameter<double>(candidate, "factor");
    const bool enabled = parameter<bool>(candidate, "enabled");

    const double length_loss = std::pow(static_cast<double>(length) - 17.0, 2.0);
    const double factor_loss = 6.0 * std::pow(factor - 2.5, 2.0);
    const double interaction = enabled ? std::abs(factor * length - 42.5) : 20.0;
    const double loss = length_loss + factor_loss + interaction;
    return loss == 0.0 ? 0.0 : -loss;
}

}  // namespace

int main() {
    using namespace pineforge::hpo;

    SearchSpace space({
        IntegerDimension("length", 5, 30),
        RealDimension("factor", 0.5, 4.0, 0.5),
        BooleanDimension("enabled"),
    });

    TpeSamplerConfig config;
    config.startup_trials = 12;

    TpeSampler sampler(space,
                       20260718,
                       ObjectiveDirection::Maximize,
                       64,
                       config,
                       CandidatePolicy::WithoutReplacement);

    double best_value = -std::numeric_limits<double>::infinity();
    Candidate best_candidate;
    while (const auto candidate = sampler.ask()) {
        const double value = evaluate(*candidate);
        sampler.tell(candidate->id, value);
        if (value > best_value) {
            best_value = value;
            best_candidate = *candidate;
        }
    }

    std::cout << "trials=" << sampler.completed() << " best=" << best_value
              << " length=" << parameter<std::int64_t>(best_candidate, "length")
              << " factor=" << parameter<double>(best_candidate, "factor")
              << " enabled=" << std::boolalpha << parameter<bool>(best_candidate, "enabled")
              << '\n';
}
