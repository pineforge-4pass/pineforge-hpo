#pragma once

#include <pineforge/pineforge.h>

#include <cstdint>
#include <string>
#include <variant>
#include <vector>

namespace pineforge::hpo {

/// One catalog fact installed through strategy_set_symbol_facts.
struct SymbolFact {
    /// Engine fact name: canonical, type, timezone, session, currency, or mintick.
    std::string field;
    /// Textual facts or the positive finite mintick value.
    std::variant<std::string, double> value;
};

/// Owned, immutable-after-loading bars for one exact request timeframe.
struct SymbolFeed {
    /// Canonical Pine timeframe spelling, such as 240 or 1D.
    std::string timeframe;
    /// Bars in strictly increasing open-time order; empty means requests read na.
    std::vector<pf_bar_t> bars;
    /// Explicit or derived closing times, one per bar.
    std::vector<std::int64_t> close_ms;
    /// SHA-256 of the engine harness's little-endian bar/close value encoding.
    std::string source_values_sha256;
};

/// Fixed other-symbol data installed into every fresh trial strategy.
struct SymbolData {
    /// Exact request.security symbol string, including exchange prefix and suffix.
    std::string symbol;
    /// Catalog facts, installed before any of this symbol's feeds.
    std::vector<SymbolFact> facts;
    /// One independently supplied feed per requested timeframe; no aggregation.
    std::vector<SymbolFeed> feeds;
};

/// Study-owned collection shared read-only by all trial workers.
using SymbolFeeds = std::vector<SymbolData>;

}
