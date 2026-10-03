#include <pineforge/hpo/pruner.hpp>

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <utility>

namespace pineforge::hpo {

Pruner::Pruner(PrunerKind kind, std::vector<double> rungs, unsigned eta, bool minimize)
    : kind_(kind), rungs_(std::move(rungs)), eta_(eta), minimize_(minimize),
      history_(rungs_.size()) {
    if (eta_ < 2)
        throw std::invalid_argument("pruner eta must be at least 2");
    double previous = 0.0;
    for (double rung : rungs_) {
        if (!std::isfinite(rung) || rung <= previous || rung >= 1.0)
            throw std::invalid_argument("pruner rungs must increase strictly in (0, 1)");
        previous = rung;
    }
    if (kind_ != PrunerKind::None && rungs_.empty())
        throw std::invalid_argument("pruning requires at least one prefix rung");
}

std::vector<std::size_t> Pruner::bar_counts(std::size_t full_size) const {
    if (full_size == 0)
        throw std::invalid_argument("pruning requires a nonempty dataset");
    std::vector<std::size_t> result;
    if (kind_ != PrunerKind::None) {
        for (double fraction : rungs_) {
            const auto count = std::max<std::size_t>(
                1, static_cast<std::size_t>(std::ceil(fraction * full_size)));
            if (count < full_size && (result.empty() || result.back() != count))
                result.push_back(count);
        }
    }
    result.push_back(full_size);
    return result;
}

std::vector<std::optional<double>> Pruner::cuts() const {
    std::vector<std::optional<double>> result(history_.size());
    if (kind_ == PrunerKind::None)
        return result;
    for (std::size_t rung = 0; rung < history_.size(); ++rung) {
        auto values = history_[rung];
        if (values.size() < eta_)
            continue;
        std::sort(values.begin(), values.end(), [&](double left, double right) {
            return minimize_ ? left < right : left > right;
        });
        if (kind_ == PrunerKind::Median || eta_ == 2) {
            const auto middle = values.size() / 2;
            result[rung] = values.size() % 2 ? values[middle]
                                           : values[middle - 1] / 2.0 + values[middle] / 2.0;
        } else {
            const auto survivors = (values.size() + eta_ - 1) / eta_;
            result[rung] = values[survivors - 1];
        }
    }
    return result;
}

void Pruner::observe(const std::vector<std::optional<double>>& scores) {
    for (std::size_t rung = 0; rung < std::min(scores.size(), history_.size()); ++rung) {
        if (scores[rung] && std::isfinite(*scores[rung]))
            history_[rung].push_back(*scores[rung]);
    }
}

bool Pruner::prune(double score, std::optional<double> cut) const noexcept {
    return cut && (minimize_ ? score > *cut : score < *cut);
}

}  // namespace pineforge::hpo
