#include <pineforge/hpo/sampler.hpp>
#include "../src/core/sha256.hpp"
#include "../src/core/sampler_checkpoint.hpp"

#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <map>
#include <sstream>
#include <stdexcept>

namespace pfh = pineforge::hpo;

std::map<std::string, pfh::SearchSpace> spaces() {
    return {
        {"linear-real", pfh::SearchSpace({pfh::RealDimension("x", -100.0, 100.0)})},
        {"log-real", pfh::SearchSpace({
            pfh::RealDimension("x", 0x1p-1022, 0x1p1023, std::nullopt, true)})},
        {"stepped-real", pfh::SearchSpace({pfh::RealDimension("x", -12.0, 14.0, 0.1)})},
        {"linear-int", pfh::SearchSpace({pfh::IntegerDimension("x", -9999, 9999, 3)})},
        {"log-int", pfh::SearchSpace({pfh::IntegerDimension("x", 1, 1000000, 1, true)})},
        {"categorical", pfh::SearchSpace({pfh::CategoricalDimension("x",
            {std::string("fast"), std::string("slow"), std::int64_t{7}, 0.25})})},
        {"bool", pfh::SearchSpace({pfh::BooleanDimension("x")})},
        {"mixed", pfh::SearchSpace({pfh::RealDimension("real", -2.0, 5.0),
            pfh::RealDimension("log", 0.01, 1000.0, std::nullopt, true),
            pfh::RealDimension("step", -10.0, 10.0, 0.25),
            pfh::IntegerDimension("int", -100, 100),
            pfh::IntegerDimension("logint", 1, 999, 1, true),
            pfh::CategoricalDimension("category", {std::string("a"), std::string("b")}),
            pfh::BooleanDimension("bool")})}
    };
}

std::string candidate_line(const pfh::Candidate& candidate) {
    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << candidate.id;
    for (const auto& entry : candidate.values) {
        output << ' ' << std::quoted(entry.first) << ' ' << entry.second.index() << ' ';
        if (const auto* real = std::get_if<double>(&entry.second)) {
            std::uint64_t bits;
            std::memcpy(&bits, real, sizeof(bits));
            output << bits;
        } else if (const auto* text = std::get_if<std::string>(&entry.second)) {
            output << std::quoted(*text);
        } else {
            std::visit([&](const auto& value) { output << value; }, entry.second);
        }
    }
    return output.str() + '\n';
}

double objective(const pfh::Candidate& candidate) {
    const auto hash = pfh::detail::sha256(candidate_line(candidate));
    return static_cast<double>(std::stoull(hash.substr(0, 12), nullptr, 16) % 10007);
}

void save(const std::filesystem::path& path, const std::string& text) {
    std::ofstream output(path, std::ios::binary);
    output << text;
    if (!output)
        throw std::runtime_error("cannot write " + path.string());
}

std::string load(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("cannot read " + path.string());
    return {std::istreambuf_iterator<char>(input), {}};
}

std::vector<pfh::WarmStartObservation> read_history(const std::string& text) {
    std::istringstream lines(text);
    lines.imbue(std::locale::classic());
    std::vector<pfh::WarmStartObservation> history;
    std::string line;
    while (std::getline(lines, line)) {
        std::istringstream input(line);
        input.imbue(std::locale::classic());
        pfh::Candidate candidate;
        input >> candidate.id;
        std::string name;
        unsigned type;
        while (input >> std::quoted(name) >> type) {
            if (type == 0) {
                std::int64_t value;
                input >> value;
                candidate.values.emplace(name, value);
            } else if (type == 1) {
                std::uint64_t bits;
                input >> bits;
                double value;
                std::memcpy(&value, &bits, sizeof(value));
                candidate.values.emplace(name, value);
            } else if (type == 2) {
                bool value;
                input >> value;
                candidate.values.emplace(name, value);
            } else if (type == 3) {
                std::string value;
                input >> std::quoted(value);
                candidate.values.emplace(name, value);
            } else {
                throw std::runtime_error("invalid parameter type in proof history");
            }
            if (!input)
                throw std::runtime_error("invalid parameter in proof history");
        }
        history.push_back({candidate, objective(candidate)});
    }
    return history;
}

std::vector<pfh::WarmStartObservation> seeded_history(const pfh::SearchSpace& space) {
    pfh::Candidate candidate;
    for (const auto& dimension : space.dimensions()) {
        std::visit([&](const auto& item) {
            using Kind = std::decay_t<decltype(item)>;
            if constexpr (std::is_same_v<Kind, pfh::RealDimension>)
                candidate.values.emplace(std::string(item.name()), item.log() ? 1.0 : 0.0);
            else if constexpr (std::is_same_v<Kind, pfh::IntegerDimension>)
                candidate.values.emplace(std::string(item.name()), item.low());
            else if constexpr (std::is_same_v<Kind, pfh::CategoricalDimension>)
                candidate.values.emplace(std::string(item.name()), item.choices().front());
            else
                candidate.values.emplace(std::string(item.name()), false);
        }, dimension);
    }
    std::vector<pfh::WarmStartObservation> history;
    for (std::uint64_t trial = 0; trial < 4200; ++trial) {
        candidate.id = trial;
        history.push_back({candidate, objective(candidate)});
    }
    return history;
}

int main(int argc, char** argv) {
    try {
        if (argc != 3 && argc != 4)
            throw std::invalid_argument("portable_math_proof OUT THREADS [INTEL_PARENT_DIR]");
        const std::filesystem::path output(argv[1]);
        std::filesystem::create_directories(output);
        pfh::TpeSamplerConfig config;
        config.max_threads = std::stoull(argv[2]);
        config.startup_trials = 12;
        config.history_switch = 100000;
        config.bad_reservoir_size = 48;
        std::string combined;
        std::string combined_identity;
        for (const auto& entry : spaces()) {
            pfh::TpeSampler sampler(entry.second, 170905,
                pfh::ObjectiveDirection::Maximize, 0, config,
                pfh::CandidatePolicy::SamplerDefault);
            std::string prefix;
            auto history = seeded_history(entry.second);
            if (argc == 4) {
                const auto parent = std::filesystem::path(argv[3]);
                prefix = load(parent / (entry.first + ".history"));
                const auto parents = read_history(prefix);
                history.insert(history.end(), parents.begin(), parents.end());
                if (parents.size() != 128 || !sampler.warm_start(history, 1,
                        load(parent / (entry.first + ".state"))))
                    throw std::runtime_error(entry.first + ": checkpoint did not restore");
                std::cout << entry.first << ": restored_sampler_state\n";
            } else {
                sampler.warm_start(history, 1);
            }
            const auto identity = pfh::detail::sampler_checkpoint_numeric_identity(
                sampler.sampler_state());
            if (combined_identity.empty())
                combined_identity = identity;
            else if (identity != combined_identity)
                throw std::runtime_error("space changed the portable numeric identity");
            save(output / (entry.first + ".identity"), identity);
            std::string proposals = prefix;
            for (std::uint64_t trial = argc == 4 ? 128 : 0; trial < 256; ++trial) {
                const auto candidate = sampler.ask();
                if (!candidate)
                    throw std::runtime_error("proof exhausted " + entry.first);
                proposals += candidate_line(*candidate);
                sampler.tell(candidate->id, objective(*candidate));
                if (trial == 127) {
                    save(output / (entry.first + ".history"), proposals);
                    save(output / (entry.first + ".state"), sampler.sampler_state());
                }
            }
            if (argc == 4 && proposals != load(std::filesystem::path(argv[3]) /
                                               (entry.first + ".proposals")))
                throw std::runtime_error(entry.first + ": restored child differs");
            save(output / (entry.first + ".proposals"), proposals);
            combined += entry.first + '\n' + proposals;
            std::cout << entry.first << ' ' << pfh::detail::sha256(proposals) << ' '
                      << pfh::detail::sha256(identity) << '\n';
        }
        const auto combined_hash = pfh::detail::sha256(combined);
        const auto identity_hash = pfh::detail::sha256(combined_identity);
        save(output / "proof.sha256", combined_hash + " " + identity_hash + '\n');
        std::cout << "aggregate " << combined_hash << ' ' << identity_hash << '\n';
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
