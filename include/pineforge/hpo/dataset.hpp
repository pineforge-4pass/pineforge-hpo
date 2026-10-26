#pragma once

#include <pineforge/pineforge.h>

#include <cstddef>
#include <filesystem>
#include <memory>
#include <vector>

namespace pineforge {
namespace hpo {

/// Verification of a binary chart's sidecar and mapped bytes.
enum class BinaryVerification { Full, Sidecar };

/// @brief Immutable owned or file-mapped OHLCV input shared by independent trial executions.
///
/// The public API intentionally exposes only const access even though the legacy PineForge C ABI
/// accepts `pf_bar_t*` rather than `const pf_bar_t*`. Timestamps must be strictly increasing and
/// owned OHLCV values must be finite. Binary inputs follow the engine's volume contract:
/// nonnegative or NaN, with finite OHLC prices. Never rewrite a mapped file in place: publish by
/// rename/os.replace. In-place rewriting or truncation is unsupported. The native runner compares
/// the open descriptor's `st_dev`, `st_ino`, `st_size`, `st_mtim` and `st_ctim` against their
/// load-time values at the end of a run, including the timeout path, before publishing results.
/// Timestamps use the full seconds+nanoseconds values the filesystem reports. Changes to device,
/// inode, size or mtime are refused. When only ctime moves (chmod, chown, xattr, an extra hard
/// link, or rename-over), the mapped bytes are re-hashed against the sidecar digest parsed at
/// load and accepted only if equal; without that digest a ctime change is refused.
/// In Sidecar mode the bytes were not compared to the digest at load, so even a metadata-only
/// change can refuse when the re-hash finds that they never matched.
/// The run's results are computed only from the bytes verified at load: a change that the mapped
/// bytes show is refused (the held mapping is re-hashed against the sidecar digest when ctime moved).
/// On platforms where a private read-only mapping does not show later in-place writes (observed on
/// macOS), the run keeps using the original verified bytes and is accepted. This also applies to a
/// same-size rewrite followed by exact mtime restoration (`touch -r`, `os.utime` with `ns=`):
/// POSIX writes and mtime updates advance ctime.
/// Residual: a rewrite that is fully restored (content and mtime) before the end-of-run re-hash
/// can go undetected if trials read the changed pages in between; the guard covers changes still
/// present at the end of the run, and the scenario needs a deliberate local writer.
/// Detection resolution is the filesystem's timestamp granularity: coarse ctime can miss a
/// rewrite within one tick that keeps size, mtime and ctime unchanged.
/// Truncation can cause an unrecoverable SIGBUS before that check and terminate the process
/// without failure JSON.
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
    /// regular `.json` sidecar (at most 1 MiB) with format `pf_bar_t/v1`, integer record_size 48,
    /// string symbol, canonical tf matching `[1-9][0-9]{0,4}|[1-9][0-9]{0,3}[DWMS]`, integer
    /// count equal to file size/48, and integer first_ts/last_ts equal to the mapped endpoints.
    /// Booleans are not integers. sha256 must contain exactly 64 lowercase hexadecimal characters.
    /// Full verification (the default) compares SHA-256 of the entire mapped file; Sidecar skips
    /// only the digest comparison. Both modes scan values and timestamp order before returning.
    /// Symbol/tf are descriptive, not compared to the run configuration; extra keys are allowed.
    /// Copies share ownership of the mapping and its open descriptor until the last is freed.
    /// Publish files by write-then-rename, never by rewriting or truncating a mapped file.
    /// @throws std::runtime_error for invalid files, sidecars, bars, or a big-endian host.
    static Dataset load_bin(const std::filesystem::path& path,
                            BinaryVerification verification = BinaryVerification::Full);

    /// Returns a const pointer suitable for PineForge backtest calls.
    const pf_bar_t* data() const noexcept {
        return mapped_bars_ ? mapped_bars_.get() : bars_.data();
    }
    /// Returns the number of bars.
    std::size_t size() const noexcept { return mapped_bars_ ? mapped_count_ : bars_.size(); }
    /// Returns whether the dataset contains no bars.
    bool empty() const noexcept { return size() == 0; }
    /// Returns the owning vector for CSV or vector-backed input.
    /// @throws std::logic_error for mapped input; use data()/size() for either format.
    const std::vector<pf_bar_t>& bars() const;

    /// Compares the mapped file's open descriptor's `st_dev`, `st_ino`, `st_size`, `st_mtim`
    /// and `st_ctim` to their load-time values, using full seconds+nanoseconds timestamps.
    ///
    /// A no-op for owned input. Changes to device, inode, size or mtime are refused. If only
    /// ctime moves (chmod, chown, xattr, an extra hard link, or rename-over), re-hashes the held
    /// mapped bytes against the sidecar digest parsed at load and accepts only equality;
    /// without a digest a ctime change is refused. Replacing the path never reopens the new file.
    /// In Sidecar mode the bytes were not compared to the digest at load, so a ctime-only
    /// change can refuse if the bytes never matched it.
    /// The run's results are computed only from the bytes verified at load: a change that the
    /// mapped bytes show is refused (the held mapping is re-hashed against the sidecar digest
    /// when ctime moved). On platforms where a private read-only mapping does not show later
    /// in-place writes (observed on macOS), the run keeps using the original verified bytes
    /// and is accepted. This also applies to a same-size rewrite followed by exact mtime
    /// restoration, as POSIX writes and mtime updates advance ctime.
    /// Detection resolution is the filesystem's timestamp granularity: coarse ctime can miss a
    /// rewrite within one tick that keeps size, mtime and ctime unchanged.
    /// Truncation can cause an unrecoverable SIGBUS before the check.
    /// @throws std::runtime_error with code hpo_dataset_invalid when the file changed while mapped
    /// or its descriptor cannot be checked.
    void verify_unchanged() const;

private:
    struct MappedFile;

    Dataset(std::shared_ptr<const MappedFile> mapped_file,
            std::shared_ptr<const pf_bar_t> mapped_bars, std::size_t count);

    std::vector<pf_bar_t> bars_;
    std::shared_ptr<const MappedFile> mapped_file_;
    std::shared_ptr<const pf_bar_t> mapped_bars_;
    std::size_t mapped_count_ = 0;
};

/// @brief Immutable, non-copying range of bars retaining its Dataset owner.
///
/// Bounds are checked once at construction without adding begin and length (so overflow cannot
/// admit an invalid range). An explicit empty view is allowed, including at owner->size(); it is
/// useful for range arithmetic but cannot be executed by TrialExecutor. A whole view preserves
/// the owner's exact data pointer. Mapped-file verification belongs to the owner at the run
/// boundary; constructing or reading a view never calls Dataset::verify_unchanged().
struct DatasetView final {
    /// Constructs a view of the entire owner.
    /// @throws std::invalid_argument with code hpo_dataset_invalid for a null owner.
    explicit DatasetView(std::shared_ptr<const Dataset> dataset);

    /// Constructs the range [first, first + count).
    /// @throws std::invalid_argument with code hpo_dataset_invalid for a null owner or bounds
    /// outside that owner's bars.
    DatasetView(std::shared_ptr<const Dataset> dataset, std::size_t first, std::size_t count);

    /// Keeps owned bars or the mapping and its descriptor alive.
    const std::shared_ptr<const Dataset> owner;
    /// First bar's index in owner.
    const std::size_t begin;
    /// Number of bars in this view.
    const std::size_t length;

    /// Returns owner->data() + begin, preserving the pointer even for an empty owner.
    const pf_bar_t* data() const noexcept;
    /// Returns the number of bars in this view.
    std::size_t size() const noexcept { return length; }
    /// Returns whether this view contains no bars.
    bool empty() const noexcept { return length == 0; }
};

}  // namespace hpo
}  // namespace pineforge
