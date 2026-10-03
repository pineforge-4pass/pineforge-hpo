#pragma once

#include <cstddef>
#include <optional>
#include <vector>

namespace pineforge::hpo {

/// Prefix pruning policy; disabled by default in the native runner.
enum class PrunerKind { None, Median, Halving };

/// Coordinator-owned rung history. Workers receive immutable cut snapshots.
/// Enabled policies retain the latest 1,024 finite observations per rung; disabled policies
/// retain none. Cut selection remains independent of worker completion timing.
class Pruner final {
public:
    /// Validates increasing fractions in (0, 1) and an elimination factor >= 2.
    Pruner(PrunerKind kind, std::vector<double> rungs, unsigned eta, bool minimize);
    /// Returns prefix lengths, including the full window, without duplicates.
    std::vector<std::size_t> bar_counts(std::size_t full_size) const;
    /// Returns cuts based only on observations already committed by the coordinator.
    std::vector<std::optional<double>> cuts() const;
    /// Adds finite rung observations after an entire batch has finished.
    void observe(const std::vector<std::optional<double>>& scores);
    /// Strictly losing scores are pruned; ties and missing cuts survive.
    bool prune(double score, std::optional<double> cut) const noexcept;

private:
    PrunerKind kind_;
    std::vector<double> rungs_;
    unsigned eta_;
    bool minimize_;
    std::vector<std::vector<double>> history_;
    std::vector<std::size_t> cursors_;
};

}  // namespace pineforge::hpo
