#include "continuation.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

namespace pfh = pineforge::hpo;
namespace detail = pineforge::hpo::detail;

namespace {

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

std::string unhex(const std::string& value) {
    std::string bytes;
    for (std::size_t index = 0; index < value.size(); index += 2)
        bytes.push_back(static_cast<char>(std::stoul(value.substr(index, 2), nullptr, 16)));
    return bytes;
}

void write(const std::filesystem::path& path, const std::string& bytes) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(bytes.data(), bytes.size());
    require(static_cast<bool>(output), "cannot write fixture");
}

void run(const std::filesystem::path& directory) {
    const std::filesystem::path fixtures(PFH_WARM_FIXTURES);
    const auto recorded = detail::space_from_spec(detail::parse_json(
        detail::read_document(fixtures / "warm_v2_spec.json")));
    const auto space = detail::search_space_from_recorded(recorded);
    const auto golden = detail::parse_json(detail::read_document(fixtures / "warm_v2_golden.json"));
    const auto single = unhex(detail::field(golden, "single_hex").text());
    const auto multiple = unhex(detail::field(golden, "multi_hex").text());
    const auto path = directory / "warm.bin";
    write(path, single);
    const auto history = detail::load_warm_history(path, space, recorded);
    require(history.binary && history.records.empty() && history.observations.empty(),
            "binary loader retained JSON or candidate maps");
    require(history.size() == 3 && history.tried_count() == 3, "wrong binary counts");
    require(history.completed == 2 && history.feasible == 1, "wrong state interpretation");
    require(history.next_id == 9007199254740994ULL, "uint64 IDs were narrowed");
    require(history.source_digest() == detail::field(golden, "single_sha256").text(),
            "binary digest differs");
    auto trials = detail::parse_json(detail::read_document(fixtures / "warm_v2_trials.json"));
    auto document = detail::object_json();
    document.members["space"] = recorded;
    document.members["trials"] = trials;
    const auto json_path = directory / "warm.json";
    write(json_path, detail::dump_json(document));
    const auto legacy = detail::load_warm_history(json_path, space, recorded);
    pfh::TpeSampler full_state(space, 73, pfh::ObjectiveDirection::Maximize);
    full_state.warm_start(legacy.observations);
    pfh::TpeSampler partial_state(space, 73, pfh::ObjectiveDirection::Maximize);
    partial_state.warm_start(std::vector<pfh::WarmStartObservation>(
        legacy.observations.begin(), legacy.observations.begin() + 1));
    const auto state_block = [](const std::string& state) {
        std::ostringstream output;
        output.write(reinterpret_cast<const char*>(detail::warm_state_magic.data()), 8);
        detail::warm_write_integer(output, static_cast<std::uint64_t>(state.size()));
        output << state;
        return output.str();
    };
    const auto full_checkpoint = full_state.sampler_state();
    const auto state_path = directory / "state.bin";
    write(state_path, single + state_block(full_checkpoint) +
                     state_block(partial_state.sampler_state()));
    const auto state_history = detail::load_warm_history(state_path, space, recorded);
    require(state_history.sampler_state == full_checkpoint,
            "out-of-order checkpoint selection used a stale state");
    pfh::TpeSampler restored(space, 73, pfh::ObjectiveDirection::Maximize);
    require(restored.warm_start(state_history.binary, 8, state_history.sampler_state),
            "binary checkpoint was not restored");
    write(state_path, single + state_block(full_checkpoint) + state_block(full_checkpoint));
    require(detail::load_warm_history(state_path, space, recorded).sampler_state == full_checkpoint,
            "identical duplicate checkpoints were rejected");
    for (std::uint64_t row = 0; row < history.size(); ++row) {
        const auto actual = history.binary->observation(space, row);
        const auto& expected = legacy.observations[row];
        require(actual.candidate.id == expected.candidate.id &&
                actual.candidate.values == expected.candidate.values &&
                actual.objective == expected.objective, "column decoder differs from JSON");
        require(history.contains(actual.candidate), "tried index lost a row");
    }
    const auto encoded = directory / "encoded.bin";
    detail::encode_warm_history(encoded, space, recorded, legacy, 0);
    require(detail::read_document(encoded) == single, "native writer differs from golden");
    detail::encode_warm_history(encoded, space, recorded, legacy, 1);
    const auto chunked = detail::load_warm_history(encoded, space, recorded);
    const auto multi_path = directory / "multi.bin";
    write(multi_path, multiple);
    const auto multi = detail::load_warm_history(multi_path, space, recorded);
    for (const auto batch : {1U, 2U, 4U, 8U}) {
        pfh::TpeSampler json(space, 73, pfh::ObjectiveDirection::Maximize, 24);
        pfh::TpeSampler binary(space, 73, pfh::ObjectiveDirection::Maximize, 24);
        pfh::TpeSampler blocks(space, 73, pfh::ObjectiveDirection::Maximize, 24);
        require(!json.warm_start(legacy.observations, batch), "unexpected JSON replay");
        require(!binary.warm_start(history.binary, batch), "unexpected binary replay");
        require(!blocks.warm_start(multi.binary, batch), "unexpected multi-block replay");
        for (std::uint64_t index = 0; index < 24; ++index) {
            const auto expected = json.ask();
            const auto actual = binary.ask();
            const auto chunk = blocks.ask();
            require(expected && actual && chunk && expected->id == actual->id &&
                    expected->values == actual->values && expected->values == chunk->values,
                    "JSON/binary/multi-block TPE suggestions differ");
            json.tell(expected->id, static_cast<double>(index));
            binary.tell(actual->id, static_cast<double>(index));
            blocks.tell(chunk->id, static_cast<double>(index));
        }
    }
    require(chunked.next_id == history.next_id, "chunked IDs differ");
    auto signed_trials = trials;
    auto positive_zero = signed_trials.items.front();
    positive_zero.members["trial_id"] = detail::Json::number("9007199254740994");
    positive_zero.members["parameters"].members["e_continuous"] = detail::Json::number("0.0");
    signed_trials.items.push_back(std::move(positive_zero));
    document.members["trials"] = std::move(signed_trials);
    const auto signed_json = directory / "signed.json";
    write(signed_json, detail::dump_json(document));
    const auto signed_legacy = detail::load_warm_history(signed_json, space, recorded);
    const auto signed_path = directory / "signed.bin";
    detail::encode_warm_history(signed_path, space, recorded, signed_legacy, 0);
    const auto signed_binary = detail::load_warm_history(signed_path, space, recorded);
    require(signed_binary.tried_count() == 4 && signed_binary.size() == 4,
            "signed-zero vectors were merged");
    for (const auto& observation : signed_legacy.observations)
        require(signed_binary.contains(observation.candidate), "signed-zero lookup differs");
    std::vector<std::string> invalid;
    for (std::size_t size = 0; size < single.size(); ++size)
        invalid.push_back(single.substr(0, size));
    invalid.push_back(single + "x");
    for (const auto offset : {0U, 8U, 10U, 12U, 16U, 24U, 32U, 36U, 40U, 44U,
                              48U, 80U, 81U, 82U, 124U, 127U}) {
        auto bytes = single;
        bytes[offset] = static_cast<char>(255);
        invalid.push_back(std::move(bytes));
    }
    auto duplicate = single;
    duplicate.replace(108, 8, single.substr(100, 8));
    invalid.push_back(duplicate);
    invalid.push_back(single + single);
    for (const auto offset : {175U, 199U, 223U}) {
        auto bytes = single;
        bytes.replace(offset, 8, unhex("000000000000f07f"));
        invalid.push_back(std::move(bytes));
    }
    for (const auto& bytes : invalid) {
        const auto malformed = directory / "malformed.bin";
        write(malformed, bytes);
        bool rejected = false;
        try {
            (void)detail::load_warm_history(malformed, space, recorded);
        } catch (const detail::WarmStartError& error) {
            rejected = std::string(error.what()).find("warm-start incompatible:") == 0;
        }
        require(rejected, "malformed binary was not rejected as incompatible");
    }
}

}

int main() {
    const auto directory = std::filesystem::temp_directory_path() /
        ("pfh-warm-v2-" + std::to_string(::getpid()));
    try {
        std::filesystem::create_directory(directory);
        run(directory);
        std::filesystem::remove_all(directory);
        std::cout << "binary golden, corruption and TPE equivalence passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::filesystem::remove_all(directory);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
