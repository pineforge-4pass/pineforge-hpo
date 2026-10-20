#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>

#include "../src/engine_adapter/dataset_bin.hpp"

#include <cmath>
#include <cstring>
#include <filesystem>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>

namespace {

using pineforge::hpo::Dataset;

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

template <typename Function>
void require_dataset_refusal(Function&& function, const std::string& cause) {
    try {
        function();
    } catch (const pineforge::hpo::HpoError& error) {
        require(error.code() == "hpo_dataset_invalid", "wrong binary dataset failure code");
        require(std::string(error.what()).find(cause) != std::string::npos,
                "expected named cause '" + cause + "', got '" + error.what() + "'");
        return;
    }
    throw std::runtime_error("binary dataset was not refused: " + cause);
}

void require_same_bars(const Dataset& actual, const Dataset& expected) {
    require(actual.size() == expected.size(), "binary/CSV bar counts differ");
    require(!actual.empty(), "good binary dataset is empty");
    for (std::size_t index = 0; index < expected.size(); ++index) {
        const auto& left = actual.data()[index];
        const auto& right = expected.data()[index];
        // Compare individual binary64 representations, including the sign of zero. The CSV
        // oracle is parsed separately; no C++ structure is used to write the binary fixture.
        for (const auto field : {&pf_bar_t::open, &pf_bar_t::high, &pf_bar_t::low,
                                 &pf_bar_t::close, &pf_bar_t::volume}) {
            require(std::memcmp(&(left.*field), &(right.*field), sizeof(double)) == 0,
                    "binary/CSV OHLCV field differs at bar " + std::to_string(index));
        }
        require(left.timestamp == right.timestamp,
                "binary/CSV timestamp differs at bar " + std::to_string(index));
    }
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: test_dataset_bin <generated-fixture-directory>");
        const std::filesystem::path directory(argv[1]);
        const Dataset csv = Dataset::load_csv(directory / "good.csv");
        std::size_t passed = 0;
        const auto test = [&](const std::string& name, const std::function<void()>& body) {
            body();
            ++passed;
            std::cout << "PASS " << name << '\n';
        };
        test("binary_layout_matches_independent_csv", [&] {
            const Dataset mapped = Dataset::load_bin(directory / "good.bin");
            require_same_bars(mapped, csv);
            require(mapped.bars().empty(), "mapped bars were copied to the owning vector");
        });
        test("binary_mapping_survives_copy_move_and_assignment", [&] {
            Dataset owner = [&] {
                const Dataset transient = Dataset::load_bin(directory / "good.bin");
                Dataset copied = transient;
                require(copied.data() == transient.data(), "dataset copy duplicated the mapping");
                return copied;
            }();
            const auto* address = owner.data();
            Dataset moved = std::move(owner);
            require(moved.data() == address, "dataset move changed the mapped address");
            require_same_bars(moved, csv);
            Dataset assigned = Dataset::load_csv(directory / "good.csv");
            assigned = moved;
            require(assigned.data() == address, "dataset assignment copied mapped bars");
            require(assigned.bars().empty(), "dataset assignment retained an owning bar vector");
            require_same_bars(assigned, csv);
        });
        test("binary_sidecar_extra_metadata_is_ignored", [&] {
            require_same_bars(Dataset::load_bin(directory / "extra-metadata.bin"), csv);
        });
        const auto refused = [&](const std::string& name, const std::string& cause) {
            test(name, [&] {
                require_dataset_refusal([&] { (void)Dataset::load_bin(directory / name); }, cause);
            });
        };
        refused("absent.bin", "cannot open OHLCV binary");
        refused("directory.bin", "not a regular file");
        refused("fifo.bin", "not a regular file");
        refused("truncated.bin", "size is not a multiple of 48");
        refused("empty.bin", "zero records");
        refused("too-many.bin", "record count exceeds 2147483647");
        refused("missing-sidecar.bin", "sidecar missing");
        refused("wrong-count.bin", "sidecar count disagrees");
        refused("wrong-format.bin", "sidecar format must be pf_bar_t/v1");
        for (const auto* type : {"bool", "float", "string", "null"}) {
            refused(std::string("count-") + type + ".bin", "sidecar count must be an integer");
        }
        refused("malformed.bin", "sidecar invalid JSON");
        refused("oversized-sidecar.bin", "sidecar invalid JSON: exceeds 1048576 bytes");
        refused("unreadable-sidecar.bin", "sidecar unreadable");
        refused("nonobject.bin", "sidecar must be a JSON object");
        refused("missing-count.bin", "sidecar count must be an integer");
        refused("missing-format.bin", "sidecar format must be pf_bar_t/v1");
        refused("exponent-count.bin", "sidecar count must be an integer");
        refused("negative-count.bin", "sidecar count disagrees");
        test("binary_big_endian_host_is_refused", [&] {
            // Exercise the production guard's refusal on the little-endian proof box. There
            // is no public flag/environment override of the loader's actual host detection.
            require_dataset_refusal(
                [] { pineforge::hpo::detail::require_bin_little_endian(false); },
                "big-endian host");
            pineforge::hpo::detail::require_bin_little_endian(true);
        });
        test("binary_stat_failure_is_named", [&] {
            require_dataset_refusal(
                [&] {
                    (void)pineforge::hpo::detail::binary_file_size(-1, directory / "good.bin");
                },
                "cannot stat OHLCV binary");
        });
        test("binary_mmap_failure_is_named", [&] {
            require_dataset_refusal(
                [&] {
                    (void)pineforge::hpo::detail::map_bin_records(-1, 48, directory / "good.bin");
                },
                "cannot mmap OHLCV binary");
        });
        test("binary_host_address_space_limit_is_named", [&] {
            require_dataset_refusal(
                [&] {
                    pineforge::hpo::detail::require_bin_address_space(
                        48, 47, directory / "good.bin");
                },
                "size exceeds host address space");
        });
        for (const auto* field : {"open", "high", "low", "close"}) {
            refused(std::string("nonfinite-") + field + ".bin", "non-finite OHLC");
        }
        refused("volume-negative.bin", "volume must be nonnegative or NaN");
        refused("volume-infinite.bin", "volume must be nonnegative or NaN");
        test("binary_nan_volume_matches_engine_contract", [&] {
            const Dataset mapped = Dataset::load_bin(directory / "volume-nan.bin");
            require(mapped.size() == csv.size(), "NaN-volume binary lost records");
            require(std::isnan(mapped.data()[1].volume), "binary NaN volume was rewritten");
        });
        refused("timestamp-duplicate.bin", "timestamps must be strictly increasing");
        refused("timestamp-descending.bin", "timestamps must be strictly increasing");
        std::cout << "dataset binary tests passed: " << passed << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "dataset binary test failure: " << error.what() << '\n';
        return 1;
    }
}
