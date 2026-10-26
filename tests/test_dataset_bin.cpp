#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>

#include "../src/core/sha256.hpp"
#include "../src/engine_adapter/dataset_bin.hpp"

#include <array>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
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

void require_mapped_bars_refusal(const Dataset& dataset) {
    try {
        (void)dataset.bars();
    } catch (const std::logic_error& error) {
        require(std::string(error.what()) ==
                    "bars() is not available for a mapped dataset; use data() and size()",
                "mapped bars() refusal does not explain the supported accessors");
        return;
    }
    throw std::runtime_error("bars() did not refuse a mapped dataset");
}

template <typename Register>
void test_mapped_identity(const std::filesystem::path& directory, Register&& test) {
    const auto path = directory / "good.bin";
    const Dataset mapped = Dataset::load_bin(path);
    const auto length = mapped.size() * sizeof(pf_bar_t);
    // Full loading already verified these real mapped bytes against the fixture's sidecar.
    const auto digest = pineforge::hpo::detail::sha256(
        std::string_view(reinterpret_cast<const char*>(mapped.data()), length));
    auto different_digest = digest;
    different_digest[0] = digest[0] == '0' ? '1' : '0';
    struct stat identity {};
    identity.st_dev = 7;
    identity.st_ino = 11;
    identity.st_size = static_cast<decltype(identity.st_size)>(length);
#if defined(__APPLE__)
    identity.st_mtimespec = {1700000000, 17};
    identity.st_ctimespec = {1700000000, 29};
#else
    identity.st_mtim = {1700000000, 17};
    identity.st_ctim = {1700000000, 29};
#endif
    auto current = identity;
#if defined(__APPLE__)
    ++current.st_ctimespec.tv_nsec;
#else
    ++current.st_ctim.tv_nsec;
#endif
    const auto verify = [&](const struct stat& status, std::string_view expected_digest) {
        pineforge::hpo::detail::require_bin_unchanged(
            status, identity, mapped.data(), length, expected_digest, path);
    };
    test("binary_unchanged_identity_does_not_require_a_digest", [&] {
        verify(identity, {});
    });
    test("binary_unchanged_identity_does_not_rehash", [&] {
        verify(identity, different_digest);
    });
    test("binary_ctime_only_matching_digest_is_accepted", [&] {
        verify(current, digest);
    });
    test("binary_ctime_only_different_digest_is_refused", [&] {
        require_dataset_refusal([&] { verify(current, different_digest); }, "changed while mapped");
    });
    test("binary_ctime_only_absent_digest_is_refused", [&] {
        require_dataset_refusal([&] { verify(current, {}); }, "changed while mapped");
    });
    auto mtime_changed = identity;
#if defined(__APPLE__)
    ++mtime_changed.st_mtimespec.tv_nsec;
#else
    ++mtime_changed.st_mtim.tv_nsec;
#endif
    const std::array<std::pair<const char*, std::string_view>, 3> digests{{
        {"matching", digest}, {"different", different_digest}, {"absent", {}},
    }};
    for (const auto& entry : digests) {
        test("binary_mtime_change_is_refused_with_" + std::string(entry.first) + "_digest", [&] {
            require_dataset_refusal([&] { verify(mtime_changed, entry.second); },
                                    "changed while mapped");
        });
    }
}

}  // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2 || (argc == 3 && std::string(argv[2]) == "--mapped-identity"),
                "usage: test_dataset_bin <generated-fixture-directory> [--mapped-identity]");
        const std::filesystem::path directory(argv[1]);
        std::size_t passed = 0;
        const auto test = [&](const std::string& name, const std::function<void()>& body) {
            body();
            ++passed;
            std::cout << "PASS " << name << '\n';
        };
        if (argc == 3) {
            test_mapped_identity(directory, test);
            std::cout << "dataset binary identity tests passed: " << passed << '\n';
            return 0;
        }
        const Dataset csv = Dataset::load_csv(directory / "good.csv");
        test("binary_layout_matches_independent_csv", [&] {
            const Dataset mapped = Dataset::load_bin(directory / "good.bin");
            require_same_bars(mapped, csv);
            require_mapped_bars_refusal(mapped);
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
            require_mapped_bars_refusal(assigned);
            require_same_bars(assigned, csv);
        });
        test("csv_bars_accessor_keeps_its_owning_vector", [&] {
            require(csv.bars().size() == csv.size(), "CSV bars() lost its owning vector");
            require(csv.bars().data() == csv.data(), "CSV bars() changed the data pointer");
        });
        test("binary_sidecar_extra_metadata_is_ignored", [&] {
            require_same_bars(Dataset::load_bin(directory / "extra-metadata.bin"), csv);
        });
        test("binary_single_record_endpoints", [&] {
            const auto single = Dataset::load_bin(directory / "single.bin");
            require(single.size() == 1 && single.data()[0].timestamp == csv.data()[0].timestamp,
                    "single-record endpoints differ");
        });
        test("binary_signed_int64_timestamp_endpoints", [&] {
            const auto wide = Dataset::load_bin(directory / "int64-timestamps.bin");
            require(wide.size() == 2 &&
                        wide.data()[0].timestamp == std::numeric_limits<std::int64_t>::min() &&
                        wide.data()[1].timestamp == std::numeric_limits<std::int64_t>::max(),
                    "int64 endpoints differ");
        });
        test("binary_sidecar_mode_skips_only_digest_comparison", [&] {
            const auto mapped = Dataset::load_bin(directory / "digest-mismatch.bin",
                                                  pineforge::hpo::BinaryVerification::Sidecar);
            require(mapped.size() == csv.size(), "sidecar mode lost records");
            require(mapped.data()[0].open != csv.data()[0].open, "flipped price byte was ignored");
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
        const std::string mismatch = "sidecar does not match the bar/close files";
        refused("wrong-count.bin", mismatch);
        refused("wrong-format.bin", mismatch);
        for (const auto* type : {"bool", "float", "string", "null"}) {
            refused(std::string("count-") + type + ".bin", mismatch);
        }
        for (const auto* name : {
                 "record-size-wrong", "record-size-missing", "record-size-bool",
                 "record-size-float", "record-size-string", "symbol-missing", "symbol-number",
                 "symbol-bool", "tf-missing", "tf-number", "first-ts-mismatch", "last-ts-mismatch",
                 "first-ts-missing", "last-ts-missing", "first-ts-bool", "last-ts-float",
                 "first-ts-string", "last-ts-overflow", "sha256-missing", "sha256-number",
                 "sha256-short", "sha256-uppercase", "sha256-nonhex", "sha256-unchecked",
                 "digest-mismatch"}) {
            refused(std::string(name) + ".bin", mismatch);
        }
        for (int index = 0; index < 12; ++index) {
            refused("tf-bad-" + std::to_string(index) + ".bin", mismatch);
        }
        refused("malformed.bin", "sidecar invalid JSON");
        refused("oversized-sidecar.bin", "sidecar invalid JSON: exceeds 1048576 bytes");
        refused("unreadable-sidecar.bin", "sidecar unreadable");
        refused("fifo-sidecar.bin", "sidecar unreadable");
        refused("nonobject.bin", "sidecar must be a JSON object");
        refused("missing-count.bin", mismatch);
        refused("missing-format.bin", mismatch);
        refused("exponent-count.bin", mismatch);
        refused("negative-count.bin", mismatch);
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
