#pragma once

// Indexed Sobol sampler (contract N1, N5-N9). UNEXECUTED source preparation (methods-sobol-mapper
// leaf, base d2f83326); see sobol_mapper.hpp for the status and build requirements.
//
// Depends on the separate engine lane for detail::SobolScramble and detail::sobol_coordinate64
// (src/core/sobol_engine.hpp); nothing of the engine is stubbed or reimplemented here.

#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>
#include <pineforge/hpo/types.hpp>

#include "sobol_engine.hpp"
#include "sobol_mapper.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace pineforge::hpo {

/// One varying dimension and the Sobol column it consumes.
struct SobolColumn {
    /// Sobol column c (0-based): the dimension's rank among the varying names in byte-wise UTF-8
    /// order. The engine maps column c to Joe-Kuo dimension c + 1.
    std::size_t index;
    /// Dimension name.
    std::string name;
    /// How the coordinate is consumed (never Constant).
    detail::SobolMapKind kind;
    /// Lattice size of a discrete kind; zero for the continuous kinds.
    std::uint64_t count;
};

/// A dimension that takes no column: integer or real with low == high, or one categorical choice.
struct SobolConstant {
    /// Dimension name.
    std::string name;
    /// The only value the dimension can take.
    ParameterValue value;
};

/// @brief Indexed, feedback-free Sobol sampler: candidate n is a pure function of the space, the seed,
/// the scramble and n, and its ID is n.
///
/// Varying dimensions are the columns, sorted by byte-wise UTF-8 name; constants take no column. Every
/// index is issued once, in order: duplicate parameter vectors are kept and nothing is rejected,
/// redrawn, reseeded or skipped. Counts are decoded per dimension, so a space whose product overflows
/// uint64, or that has continuous dimensions, is accepted.
///
/// Index range: the generator accepts every uint64, but trial ID 2^64-1 is reserved (HPO leaves it
/// free so a continuation always has a next ID). The sampler therefore issues indices
/// first_index .. 2^64-2 at most, refuses first_index == 2^64-1 and a budget above 2^64-1-first_index,
/// and at() refuses index 2^64-1.
///
/// at() and the metadata accessors are const and thread-safe. next() and reset() mutate the cursor and
/// need external serialization, like the other samplers' next().
class SobolSampler final : public Sampler {
public:
    /// Builds the sampler.
    /// @param seed digital-shift seed (unsigned 64-bit); ignored by SobolScramble::None.
    /// @param first_index index of the first candidate (a continuation starts at the parent's next ID).
    /// @param max_candidates number of candidates to issue; 0 means every remaining issuable index.
    /// @throws TypedHpoError<std::invalid_argument> (hpo_study_spec_invalid, reason sampler) for more
    ///         than 1024 varying dimensions, first_index == 2^64-1, a budget above 2^64-1-first_index,
    ///         or a log-integer range outside 1 <= low <= 2^52 and high - low + 1 <= 2^53.
    /// @throws TypedHpoError<std::overflow_error> (same code and reason) for an integer dimension with
    ///         2^64 values.
    SobolSampler(SearchSpace space,
                 std::uint64_t seed,
                 detail::SobolScramble scramble = detail::SobolScramble::DigitalShift,
                 std::uint64_t first_index = 0,
                 std::uint64_t max_candidates = 0);

    /// Returns the candidate at cursor() and advances, or std::nullopt after limit() candidates.
    std::optional<Candidate> next() override;
    /// Rewinds the cursor to first_index(); points never change.
    void reset() override;
    /// Returns the number of candidates issued since construction or the last reset.
    std::uint64_t generated() const noexcept override { return issued_; }

    /// Returns the candidate with ID == @p index, whatever the cursor.
    /// @throws TypedHpoError<std::out_of_range> (hpo_invariant) for the reserved index 2^64-1.
    Candidate at(std::uint64_t index) const;

    /// Returns the digital-shift seed as given (meaningful only for DigitalShift).
    std::uint64_t seed() const noexcept { return seed_; }
    /// Returns the scramble.
    detail::SobolScramble scramble() const noexcept { return scramble_; }
    /// Returns the index of the first candidate this sampler issues.
    std::uint64_t first_index() const noexcept { return first_index_; }
    /// Returns the next index next() would issue (first_index() + generated(); at most 2^64-1).
    std::uint64_t cursor() const noexcept { return first_index_ + issued_; }
    /// Returns the number of candidates this sampler issues in total.
    std::uint64_t limit() const noexcept { return limit_; }
    /// Returns the varying columns in column order (empty when nothing varies).
    const std::vector<SobolColumn>& columns() const noexcept { return columns_; }
    /// Returns the constant dimensions in declaration order.
    const std::vector<SobolConstant>& constants() const noexcept { return constants_; }
    /// Returns the search space the sampler was built from.
    const SearchSpace& space() const noexcept { return space_; }
    /// True when any column passes through binary64 math (stepped real, linear/log real, log integer),
    /// i.e. when the Sobol numeric build identity applies (N11); false for discrete-only columns.
    bool uses_floating_point() const noexcept { return floating_point_; }

private:
    SearchSpace space_;
    std::uint64_t seed_;
    detail::SobolScramble scramble_;
    std::uint64_t first_index_;
    std::uint64_t limit_ = 0;
    std::uint64_t issued_ = 0;
    bool floating_point_ = false;
    std::vector<detail::SobolDimensionMap> maps_;  // varying dimensions, in column order
    std::vector<SobolColumn> columns_;
    std::vector<SobolConstant> constants_;
};

}  // namespace pineforge::hpo
