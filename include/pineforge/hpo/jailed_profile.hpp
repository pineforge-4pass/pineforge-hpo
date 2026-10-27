#pragma once

#include <pineforge/hpo/strategy_plugin.hpp>

namespace pineforge::hpo::detail {

/// Detects the engine's presence-based inherited-arena contract, including an empty value.
bool jailed_profile_requested() noexcept;

/// With the arena variable present, installs bounded tables and then admits the inherited arena.
/// Otherwise performs no plugin call. Engine refusals propagate unchanged to the caller.
void prepare_jailed_handle(const StrategyPlugin& plugin, pf_strategy_t strategy);

}  // namespace pineforge::hpo::detail
