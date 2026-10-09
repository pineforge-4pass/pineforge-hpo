// UNEXECUTED source preparation (methods-sobol-mapper leaf, base d2f83326); see sobol_sampler.hpp.
#include "sobol_sampler.hpp"

#include <pineforge/hpo/error.hpp>

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <utility>

namespace pineforge::hpo {
namespace {

// Trial ID 2^64-1 is never issued: HPO's existing uint64 rules (json_id, the preflight in main.cpp
// and available_ids) leave it free so a continuation always has a next ID.
constexpr std::uint64_t kReservedIndex = std::numeric_limits<std::uint64_t>::max();

}  // namespace

SobolSampler::SobolSampler(SearchSpace space,
                           std::uint64_t seed,
                           detail::SobolScramble scramble,
                           std::uint64_t first_index,
                           std::uint64_t max_candidates)
    : space_(std::move(space)), seed_(seed), scramble_(scramble), first_index_(first_index) {
    if (first_index_ == kReservedIndex) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "sobol first index 2^64-1 is a reserved trial ID and leaves no index to issue");
    }
    // Indices first_index .. 2^64-2 are issuable: exactly 2^64-1-first_index of them.
    const std::uint64_t available = kReservedIndex - first_index_;
    if (max_candidates > available) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "sobol budget exceeds the trial IDs remaining after the first index");
    }
    limit_ = max_candidates == 0 ? available : max_candidates;

    std::vector<detail::SobolDimensionMap> varying;
    for (const Dimension& dimension : space_.dimensions()) {
        detail::SobolDimensionMap map(dimension);
        if (map.varying()) {
            varying.push_back(std::move(map));
        } else {
            constants_.push_back(SobolConstant{map.name(), map.value_at(0)});
        }
    }
    if (varying.size() > detail::kSobolMaxColumns) {
        throw TypedHpoError<std::invalid_argument>(
            "hpo_study_spec_invalid", {{"reason", "sampler"}},
            "sobol supports at most 1024 varying dimensions");
    }
    // Byte-wise UTF-8 order: std::string compares as unsigned char. Names are unique, so the order
    // is total and independent of declaration order.
    std::sort(varying.begin(), varying.end(),
              [](const detail::SobolDimensionMap& left, const detail::SobolDimensionMap& right) {
                  return left.name() < right.name();
              });
    maps_ = std::move(varying);
    columns_.reserve(maps_.size());
    for (std::size_t column = 0; column < maps_.size(); ++column) {
        columns_.push_back(
            SobolColumn{column, maps_[column].name(), maps_[column].kind(), maps_[column].count()});
        floating_point_ = floating_point_ || detail::sobol_map_uses_floating_point(maps_[column].kind());
    }
}

Candidate SobolSampler::at(std::uint64_t index) const {
    if (index == kReservedIndex) {
        throw TypedHpoError<std::out_of_range>(
            "hpo_invariant", {}, "sobol index 2^64-1 is a reserved trial ID and is never issued");
    }
    Candidate candidate;
    candidate.id = index;
    for (const SobolConstant& constant : constants_) {
        candidate.values.emplace(constant.name, constant.value);
    }
    for (std::size_t column = 0; column < maps_.size(); ++column) {
        const std::uint64_t coordinate =
            detail::sobol_coordinate64(index, column, seed_, scramble_);
        candidate.values.emplace(maps_[column].name(), maps_[column].value_at(coordinate));
    }
    return candidate;
}

std::optional<Candidate> SobolSampler::next() {
    if (issued_ >= limit_) {
        return std::nullopt;
    }
    // first_index_ + issued_ <= first_index_ + limit_ - 1 <= 2^64-2: no wrap, never the reserved ID.
    Candidate candidate = at(first_index_ + issued_);
    ++issued_;
    return candidate;
}

void SobolSampler::reset() {
    issued_ = 0;
}

}  // namespace pineforge::hpo
