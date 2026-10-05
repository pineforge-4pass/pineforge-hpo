#include <pineforge/hpo/sampler.hpp>
#include "../../src/core/sha256.hpp"

#include <chrono>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <sys/resource.h>
#include <vector>

namespace pfh = pineforge::hpo;
using Clock = std::chrono::steady_clock;

class History final : public pfh::WarmStartSource {
public:
    History(std::uint64_t count, std::size_t dimensions, const std::string& fixture = {})
        : count_(count), dimensions_(dimensions) {
        if (fixture.empty())
            return;
        std::ifstream input(fixture, std::ios::binary);
        if (!input)
            throw std::runtime_error("cannot open replay fixture");
        while (prefix_.size() < count * dimensions) {
            std::uint64_t bits;
            if (!input.read(reinterpret_cast<char*>(&bits), sizeof(bits)))
                break;
            if (dimensions == 32 && prefix_.size() % dimensions < 4) {
                prefix_.emplace_back(static_cast<std::int64_t>(bits));
            } else {
                double value;
                std::memcpy(&value, &bits, sizeof(value));
                prefix_.emplace_back(value);
            }
        }
        if (prefix_.size() % dimensions)
            throw std::runtime_error("truncated replay fixture");
    }
    std::uint64_t size() const noexcept override { return count_; }
    std::uint64_t id(std::uint64_t row) const override { return row; }
    pfh::ParameterValue parameter(std::uint64_t row, std::size_t column) const override {
        if (row * dimensions_ + column < prefix_.size())
            return prefix_[row * dimensions_ + column];
        if (dimensions_ == 32 && column < 4)
            return static_cast<std::int64_t>((row * 500009 + column * 73) % 2000003);
        return static_cast<double>((row * 1000003 + column * 73) % (1U << 21U)) /
               static_cast<double>(1U << 21U);
    }
    std::optional<double> objective(std::uint64_t row) const override {
        return static_cast<double>(row % 997);
    }
private:
    std::uint64_t count_;
    std::size_t dimensions_;
    std::vector<pfh::ParameterValue> prefix_;
};

void record(std::ostream& output, const pfh::Candidate& candidate) {
    output << candidate.id << ':';
    for (const auto& entry : candidate.values) {
        output << entry.first << ':' << entry.second.index() << ':';
        if (const auto* value = std::get_if<double>(&entry.second)) {
            std::uint64_t bits;
            std::memcpy(&bits, value, sizeof(bits));
            output << bits;
        } else {
            output << std::get<std::int64_t>(entry.second);
        }
        output << ';';
    }
    output << '\n';
}

int main(int argc, char** argv) {
    try {
        if (argc != 5 && argc != 6)
            throw std::invalid_argument("profile MODE DIMENSIONS HISTORY SWITCH [FIXTURE]");
        const std::string mode = argv[1];
        const auto dimensions = static_cast<std::size_t>(std::stoull(argv[2]));
        const std::uint64_t count = std::stoull(argv[3]);
        pfh::TpeSamplerConfig config;
        const auto history_switch = std::stoull(argv[4]);
        if (history_switch)
            config.history_switch = history_switch;
        std::vector<pfh::Dimension> descriptors;
        for (std::size_t column = 0; column < dimensions; ++column) {
            const auto name = "P" + std::to_string(column);
            if (dimensions == 32 && column < 4)
                descriptors.emplace_back(pfh::IntegerDimension(name, 0, 2000002));
            else
                descriptors.emplace_back(pfh::RealDimension(name, 0.0, 1.0));
        }
        const pfh::SearchSpace space(std::move(descriptors));
        const std::string fixture = argc == 6 ? argv[5] : "";
        auto source = std::make_shared<History>(count, dimensions,
                                               mode == "prepare" ? "" : fixture);
        pfh::TpeSampler sampler(space, 73, pfh::ObjectiveDirection::Minimize, 0, config);
        std::ostringstream trace;
        double import_seconds = 0;
        double proposal_seconds = 0;
        const auto started = Clock::now();
        if (mode == "replay" || mode == "sequence" || mode == "prepare") {
            std::ofstream fixture_output;
            if (mode == "prepare") {
                fixture_output.open(fixture, std::ios::binary);
                if (!fixture_output)
                    throw std::runtime_error("cannot write replay fixture");
            }
            for (std::uint64_t begin = 0; begin < count; begin += 8) {
                std::vector<pfh::Candidate> pending;
                for (std::uint64_t index = begin; index < std::min(count, begin + 8); ++index) {
                    auto candidate = *sampler.ask();
                    record(trace, candidate);
                    if (mode == "prepare") {
                        for (const auto& descriptor : space.dimensions()) {
                            const auto& value = candidate.values.at(
                                std::string(pfh::dimension_name(descriptor)));
                            std::uint64_t bits;
                            if (const auto* real = std::get_if<double>(&value))
                                std::memcpy(&bits, real, sizeof(bits));
                            else
                                bits = static_cast<std::uint64_t>(std::get<std::int64_t>(value));
                            fixture_output.write(reinterpret_cast<const char*>(&bits),
                                                 sizeof(bits));
                        }
                    }
                    pending.push_back(std::move(candidate));
                }
                for (const auto& candidate : pending)
                    sampler.tell(candidate.id, static_cast<double>(candidate.id % 997));
                if (begin % 1024 == 0)
                    std::cerr << "replay_prefix=" << begin + pending.size() << '\n';
            }
            import_seconds = std::chrono::duration<double>(Clock::now() - started).count();
        } else {
            if (mode == "checkpoint") {
#if !defined(PFH_NOREPLAY_BASELINE)
                pfh::TpeSampler parent(space, 73, pfh::ObjectiveDirection::Minimize, 0, config);
                parent.warm_start(source);
                const auto state = parent.sampler_state();
                const auto begin = Clock::now();
                if (!sampler.warm_start(source, 8, state))
                    throw std::runtime_error("checkpoint not restored");
                import_seconds = std::chrono::duration<double>(Clock::now() - begin).count();
#else
                throw std::runtime_error("baseline has no checkpoint API");
#endif
            } else {
                sampler.warm_start(source, mode == "legacy" ? 8 : 0);
                import_seconds = std::chrono::duration<double>(Clock::now() - started).count();
            }
        }
        const auto begin = Clock::now();
        const auto proposals = mode == "prepare" ? 0U : mode == "batch" ? 8U :
                               mode == "sequence" ? 16U : 1U;
        std::vector<pfh::Candidate> pending;
        for (unsigned index = 0; index < proposals; ++index) {
            const auto candidate = sampler.ask();
            if (!candidate)
                throw std::runtime_error("proposal missing");
            record(trace, *candidate);
            pending.push_back(*candidate);
        }
        for (const auto& candidate : pending)
            sampler.tell(candidate.id, static_cast<double>(candidate.id % 997));
        proposal_seconds = std::chrono::duration<double>(Clock::now() - begin).count();
        struct rusage usage {};
        getrusage(RUSAGE_SELF, &usage);
        std::cout << std::setprecision(12) << "{\"mode\":\"" << mode << "\",\"inputs\":"
                  << dimensions << ",\"history\":" << count << ",\"history_switch\":"
                  << history_switch << ",\"import_s\":" << import_seconds
                  << ",\"proposals_s\":" << proposal_seconds << ",\"first_new_s\":"
                  << import_seconds + proposal_seconds << ",\"proposals\":" << proposals
                  << ",\"rss_kib\":" << usage.ru_maxrss << ",\"suggestion_sha256\":\""
                  << pfh::detail::sha256(trace.str()) << "\"}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
