#pragma once

#include <optional>
#include <string>

namespace pineforge::hpo::detail {

using TpeLogRatioObserver = void (*)(double);
void set_tpe_log_ratio_observer(TpeLogRatioObserver observer) noexcept;
void set_tpe_contraction_override(std::optional<double> value) noexcept;
std::string tpe_numeric_identity();

}
