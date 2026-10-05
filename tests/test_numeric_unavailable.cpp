#include <pineforge/hpo/sampler.hpp>
#include "../src/core/numeric_build.hpp"

#include <cerrno>
#include <iostream>
#include <stdexcept>

int main() {
    try {
        errno = EDOM;
        const auto& fingerprint = pineforge::hpo::detail::runtime_math_fingerprint();
        if (errno != EDOM || &fingerprint !=
            &pineforge::hpo::detail::runtime_math_fingerprint())
            throw std::runtime_error("math probe is not cached or changed errno");
        const pineforge::hpo::SearchSpace space({pineforge::hpo::RealDimension("value", 0.0, 1.0)});
        pineforge::hpo::TpeSampler parent(space, 17);
        const auto state = parent.sampler_state();
        if (state.find(";flags_sha256:unavailable") == std::string::npos)
            throw std::runtime_error("test did not simulate an unavailable flags hash");
        pineforge::hpo::TpeSampler child(space, 17);
        if (child.warm_start(std::vector<pineforge::hpo::WarmStartObservation>{}, 1, state))
            throw std::runtime_error("matching unavailable flags identities restored");
        std::cout << "PASS: matching flags_sha256:unavailable never restores; cached math probe\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
