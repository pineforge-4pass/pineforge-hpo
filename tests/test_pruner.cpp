#include <pineforge/hpo/pruner.hpp>

#include <stdexcept>
#include <vector>

namespace pfh = pineforge::hpo;

void require(bool condition) {
    if (!condition)
        throw std::runtime_error("pruner regression");
}

int main() {
    pfh::Pruner median(pfh::PrunerKind::Median, {0.25, 0.5}, 2, false);
    require(!median.cuts()[0]);
    median.observe({2.0, 9.0});
    require(!median.cuts()[0]);
    median.observe({6.0, 1.0});
    const auto frozen = median.cuts();
    require(*frozen[0] == 4.0 && *frozen[1] == 5.0);
    require(median.prune(3.0, frozen[0]));
    require(!median.prune(4.0, frozen[0]));
    median.observe({100.0, 100.0});
    require(*frozen[0] == 4.0);

    pfh::Pruner halving(pfh::PrunerKind::Halving, {0.25}, 3, true);
    halving.observe({9.0});
    halving.observe({3.0});
    halving.observe({6.0});
    require(*halving.cuts()[0] == 3.0);
    require(halving.prune(6.0, halving.cuts()[0]));
    require(!halving.prune(3.0, halving.cuts()[0]));
    halving.observe({std::nullopt});
    require(*halving.cuts()[0] == 3.0);
    require(median.bar_counts(3) == std::vector<std::size_t>({1, 2, 3}));
    require(median.bar_counts(1) == std::vector<std::size_t>({1}));
    pfh::Pruner rolling(pfh::PrunerKind::Median, {0.5}, 2, false);
    pfh::Pruner disabled(pfh::PrunerKind::None, {0.5}, 2, false);
    for (unsigned index = 0; index < 2000; ++index) {
        rolling.observe({static_cast<double>(index)});
        disabled.observe({static_cast<double>(index)});
    }
    require(*rolling.cuts()[0] == 1487.5);
    require(!disabled.cuts()[0]);
    return 0;
}
