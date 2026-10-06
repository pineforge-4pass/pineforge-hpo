#pragma once

#include <optional>
#include <string>
#include <pineforge/hpo/search_space.hpp>

namespace pineforge::hpo::detail {

using TpeLogRatioObserver = void (*)(double);
void set_tpe_log_ratio_observer(TpeLogRatioObserver observer) noexcept;
void set_tpe_contraction_override(std::optional<double> value) noexcept;
std::string tpe_numeric_identity();
std::string tpe_numeric_identity(const SearchSpace& space);
double tpe_log_normal_interval(double lower, double upper);

}
