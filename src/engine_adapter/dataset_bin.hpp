#pragma once

#include <pineforge/pineforge.h>

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>

namespace pineforge::hpo::detail {

// Internal production guard, separately callable so an x86 test can reach the endian refusal.
// Dataset::load_bin always supplies the actual host byte order; no user override exists.
void require_bin_little_endian(bool little_endian);

// The loader supplies SIZE_MAX; tests can reach the narrow-host refusal on an x86 box.
void require_bin_address_space(std::uint64_t bytes, std::uint64_t maximum,
                               const std::filesystem::path& path);

// Small OS boundaries shared by the loader and failure tests using an invalid descriptor.
std::uint64_t binary_file_size(int descriptor, const std::filesystem::path& path);
std::shared_ptr<const pf_bar_t> map_bin_records(int descriptor, std::size_t length,
                                             const std::filesystem::path& path);

}  // namespace pineforge::hpo::detail
