#include <pineforge/hpo/jailed_profile.hpp>

#include <cstdlib>

namespace pineforge::hpo::detail {

bool jailed_profile_requested() noexcept {
    return std::getenv("PF_SCRATCH_ARENA_FD") != nullptr;
}

void prepare_jailed_handle(const StrategyPlugin& plugin, pf_strategy_t strategy) {
    if (!jailed_profile_requested())
        return;
    plugin.set_scratch_tables_bounded_default(strategy);
    plugin.require_scratch_arena(strategy);
}

}  // namespace pineforge::hpo::detail
