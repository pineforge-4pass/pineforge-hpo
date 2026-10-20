#pragma once

#include <pineforge/pineforge.h>

#include <cstddef>
#include <filesystem>
#include <memory>
#include <vector>

namespace pineforge {
namespace hpo {

/// @brief Immutable owned or file-mapped OHLCV input shared by independent trial executions.
///
/// The public API intentionally exposes only const access even though the legacy PineForge C ABI
/// accepts `pf_bar_t*` rather than `const pf_bar_t*`. Timestamps must be strictly increasing and
/// owned OHLCV values must be finite. Binary inputs follow the engine's volume contract:
/// nonnegative or NaN, with finite OHLC prices. Mapped files must remain immutable while in use.
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

    /// Maps a headerless little-endian `pf_bar_t/v1` file read-only, without copying its bars.
    ///
    /// Requires a regular file containing 1..2147483647 records of 48 bytes and an appended
    /// `.json` sidecar whose format and integer count match. Other sidecar fields, including
    /// digests, are not checked. Copies share ownership of the mapping until the last is freed.
    /// @throws std::runtime_error for invalid files, sidecars, bars, or a big-endian host.
    static Dataset load_bin(const std::filesystem::path& path);

    /// Returns a const pointer suitable for PineForge backtest calls.
    const pf_bar_t* data() const noexcept {
        return mapped_bars_ ? mapped_bars_.get() : bars_.data();
    }
    /// Returns the number of bars.
    std::size_t size() const noexcept { return mapped_bars_ ? mapped_count_ : bars_.size(); }
    /// Returns whether the dataset contains no bars.
    bool empty() const noexcept { return size() == 0; }
    /// Returns the owning vector; empty for mapped input. Use data()/size() for either format.
    const std::vector<pf_bar_t>& bars() const noexcept { return bars_; }

private:
    Dataset(std::shared_ptr<const pf_bar_t> mapped_bars, std::size_t count);

    std::vector<pf_bar_t> bars_;
    std::shared_ptr<const pf_bar_t> mapped_bars_;
    std::size_t mapped_count_ = 0;
};

}  // namespace hpo
}  // namespace pineforge
