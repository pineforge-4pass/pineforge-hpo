#pragma once

#include <pineforge/pineforge.h>

#include <cstddef>
#include <filesystem>
#include <vector>

namespace pineforge {
namespace hpo {

/// @brief Immutable in-memory OHLCV input shared by independent trial executions.
///
/// The public API intentionally exposes only const access even though the legacy PineForge C ABI
/// accepts `pf_bar_t*` rather than `const pf_bar_t*`. Timestamps must be strictly increasing and
/// every OHLCV value must be finite.
class Dataset final {
public:
    /// Takes ownership of validated bars.
    /// @throws std::invalid_argument for non-finite values or non-increasing timestamps.
    explicit Dataset(std::vector<pf_bar_t> bars);

    /// Loads `timestamp,open,high,low,close,volume` columns from a CSV file.
    ///
    /// Header matching is ASCII case-insensitive, blank data lines are ignored, and quoted CSV
    /// fields are supported. The result contains at least one bar.
    /// @throws std::runtime_error when the file cannot be read or its CSV data is invalid.
    /// @throws std::invalid_argument when parsed bars violate Dataset invariants.
    static Dataset load_csv(const std::filesystem::path& path);

    /// Returns a const pointer suitable for PineForge backtest calls.
    const pf_bar_t* data() const noexcept { return bars_.data(); }
    /// Returns the number of bars.
    std::size_t size() const noexcept { return bars_.size(); }
    /// Returns whether the dataset contains no bars.
    bool empty() const noexcept { return bars_.empty(); }
    /// Returns the immutable owning bar container.
    const std::vector<pf_bar_t>& bars() const noexcept { return bars_; }

private:
    std::vector<pf_bar_t> bars_;
};

}  // namespace hpo
}  // namespace pineforge
