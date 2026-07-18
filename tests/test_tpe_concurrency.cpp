#include <pineforge/hpo/sampler.hpp>
#include <pineforge/hpo/search_space.hpp>

#include <atomic>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <mutex>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

pineforge::hpo::SearchSpace make_space() {
    using namespace pineforge::hpo;
    return SearchSpace(
        {IntegerDimension("period", 1, 200), RealDimension("factor", 0.01, 10.0),
         BooleanDimension("enabled"),
         CategoricalDimension("mode", {std::string("a"), std::string("b"), std::string("c")})});
}

double objective(const pineforge::hpo::Candidate& candidate) {
    const auto period = std::get<std::int64_t>(*candidate.find("period"));
    const auto factor = std::get<double>(*candidate.find("factor"));
    const auto enabled = std::get<bool>(*candidate.find("enabled"));
    const auto& mode = std::get<std::string>(*candidate.find("mode"));
    return -std::pow(static_cast<double>(period) - 37.0, 2.0) - std::pow(factor - 2.5, 2.0) +
           (enabled ? 1.0 : -1.0) + (mode == "b" ? 2.0 : 0.0);
}

void test_concurrent_lifecycle() {
    using namespace pineforge::hpo;

    constexpr std::uint64_t kTrials = 256;
    constexpr unsigned kThreads = 8;
    const SearchSpace space = make_space();
    TpeSamplerConfig config;
    config.startup_trials = 8;
    config.ei_candidates = 16;
    config.constant_liar = true;
    TpeSampler sampler(space, 0xC0FFEEU, ObjectiveDirection::Maximize, kTrials, config);

    std::mutex records_mutex;
    std::set<std::uint64_t> ids;
    std::string first_error;
    std::atomic<std::uint64_t> told{0};
    std::atomic<std::uint64_t> abandoned{0};
    std::vector<std::thread> threads;
    threads.reserve(kThreads);

    for (unsigned thread = 0; thread < kThreads; ++thread) {
        threads.emplace_back([&]() {
            try {
                while (const auto candidate = sampler.ask()) {
                    if (!space.is_valid(*candidate)) {
                        throw std::runtime_error("TPE produced an invalid concurrent candidate");
                    }
                    {
                        std::lock_guard<std::mutex> lock(records_mutex);
                        if (!ids.insert(candidate->id).second) {
                            throw std::runtime_error("TPE produced a duplicate concurrent id");
                        }
                    }

                    if (candidate->id % 7 == 0) {
                        sampler.abandon(candidate->id);
                        abandoned.fetch_add(1, std::memory_order_relaxed);
                    } else {
                        sampler.tell(candidate->id, objective(*candidate));
                        told.fetch_add(1, std::memory_order_relaxed);
                    }
                }
            } catch (const std::exception& error) {
                std::lock_guard<std::mutex> lock(records_mutex);
                if (first_error.empty()) {
                    first_error = error.what();
                }
            }
        });
    }
    for (auto& thread : threads) {
        thread.join();
    }

    require(first_error.empty(), "concurrent lifecycle failed: " + first_error);
    require(ids.size() == kTrials, "concurrent lifecycle did not generate the full budget");
    for (std::uint64_t id = 0; id < kTrials; ++id) {
        require(ids.count(id) == 1, "concurrent lifecycle left a gap in candidate ids");
    }
    require(
        told.load(std::memory_order_relaxed) + abandoned.load(std::memory_order_relaxed) == kTrials,
        "concurrent lifecycle lost a terminal transition");
    require(sampler.generated() == kTrials, "generated counter drifted under concurrency");
    require(sampler.completed() == told.load(std::memory_order_relaxed),
            "completed counter drifted under concurrency");
    require(sampler.outstanding() == 0, "outstanding requests remained after concurrency test");
}

void test_concurrent_reset_rejection_preserves_pending() {
    using namespace pineforge::hpo;

    constexpr unsigned kThreads = 8;
    const SearchSpace space = make_space();
    TpeSampler sampler(space, 314159U, ObjectiveDirection::Maximize, 16);
    const auto pending = sampler.ask();
    require(pending.has_value(), "failed to create pending request for reset regression");
    require(sampler.generated() == 1 && sampler.completed() == 0 && sampler.outstanding() == 1,
            "pending request counters were not initialized");

    std::atomic<unsigned> ready{0};
    std::atomic<bool> start{false};
    std::atomic<unsigned> rejected{0};
    std::atomic<unsigned> succeeded{0};
    std::atomic<unsigned> wrong_exception{0};
    std::vector<std::thread> threads;
    threads.reserve(kThreads);
    for (unsigned thread = 0; thread < kThreads; ++thread) {
        threads.emplace_back([&]() {
            ready.fetch_add(1, std::memory_order_release);
            while (!start.load(std::memory_order_acquire)) {
                std::this_thread::yield();
            }
            try {
                sampler.reset();
                succeeded.fetch_add(1, std::memory_order_relaxed);
            } catch (const std::logic_error& error) {
                if (std::string(error.what()).find("outstanding") != std::string::npos) {
                    rejected.fetch_add(1, std::memory_order_relaxed);
                } else {
                    wrong_exception.fetch_add(1, std::memory_order_relaxed);
                }
            } catch (...) {
                wrong_exception.fetch_add(1, std::memory_order_relaxed);
            }
        });
    }
    while (ready.load(std::memory_order_acquire) != kThreads) {
        std::this_thread::yield();
    }
    start.store(true, std::memory_order_release);
    for (auto& thread : threads) {
        thread.join();
    }

    require(rejected.load(std::memory_order_relaxed) == kThreads,
            "not every concurrent reset explicitly rejected the pending request");
    require(succeeded.load(std::memory_order_relaxed) == 0,
            "reset succeeded while a request was outstanding");
    require(wrong_exception.load(std::memory_order_relaxed) == 0,
            "reset used an unexpected exception contract");
    require(sampler.generated() == 1 && sampler.completed() == 0 && sampler.outstanding() == 1,
            "rejected reset mutated or cleared the pending request");

    sampler.tell(pending->id, objective(*pending));
    require(sampler.generated() == 1 && sampler.completed() == 1 && sampler.outstanding() == 0,
            "pending request could not complete after rejected resets");
    sampler.reset();
    require(sampler.generated() == 0 && sampler.completed() == 0 && sampler.outstanding() == 0,
            "reset did not restore the idle sampler state");
    const auto replay = sampler.ask();
    require(replay.has_value() && replay->id == 0, "reset did not restore candidate ids");
    sampler.abandon(replay->id);
}

void test_concurrent_finite_exhaustion_with_partial_batch() {
    using namespace pineforge::hpo;

    constexpr unsigned kWorkers = 8;
    const SearchSpace space(
        {IntegerDimension("x", 0, 4), BooleanDimension("enabled"),
         CategoricalDimension("mode", {std::string("a"), std::string("b"), std::string("c")})});
    const auto cardinality = space.finite_cardinality();
    require(cardinality.has_value() && *cardinality == 30,
            "finite concurrency fixture cardinality is not 30");

    TpeSamplerConfig config;
    config.startup_trials = 4;
    config.ei_candidates = 8;
    TpeSampler sampler(space, 0xF17EULL, ObjectiveDirection::Maximize, *cardinality, config,
                       CandidatePolicy::Exhaustive);

    std::set<std::uint64_t> ids;
    std::set<std::uint64_t> ordinals;
    std::size_t final_nonempty_batch = 0;
    std::uint64_t told = 0;
    std::uint64_t abandoned = 0;

    for (;;) {
        std::vector<std::optional<Candidate>> batch(kWorkers);
        std::vector<std::string> errors(kWorkers);
        std::atomic<unsigned> ready{0};
        std::atomic<bool> start{false};
        std::vector<std::thread> ask_threads;
        ask_threads.reserve(kWorkers);
        for (unsigned worker = 0; worker < kWorkers; ++worker) {
            ask_threads.emplace_back([&, worker]() {
                ready.fetch_add(1, std::memory_order_release);
                while (!start.load(std::memory_order_acquire)) {
                    std::this_thread::yield();
                }
                try {
                    batch[worker] = sampler.ask();
                } catch (const std::exception& error) {
                    errors[worker] = error.what();
                }
            });
        }
        while (ready.load(std::memory_order_acquire) != kWorkers) {
            std::this_thread::yield();
        }
        start.store(true, std::memory_order_release);
        for (auto& thread : ask_threads) {
            thread.join();
        }

        std::vector<Candidate> candidates;
        for (unsigned worker = 0; worker < kWorkers; ++worker) {
            require(errors[worker].empty(), "finite concurrent ask failed: " + errors[worker]);
            if (!batch[worker].has_value()) {
                continue;
            }
            Candidate candidate = std::move(*batch[worker]);
            require(ids.insert(candidate.id).second,
                    "finite concurrent ask produced a duplicate candidate id");
            require(ordinals.insert(space.candidate_ordinal(candidate)).second,
                    "finite concurrent ask repeated a pending or attempted ordinal");
            candidates.push_back(std::move(candidate));
        }

        if (candidates.empty()) {
            break;
        }
        final_nonempty_batch = candidates.size();

        std::vector<std::string> terminal_errors(candidates.size());
        std::vector<std::thread> terminal_threads;
        terminal_threads.reserve(candidates.size());
        for (std::size_t index = 0; index < candidates.size(); ++index) {
            terminal_threads.emplace_back([&, index]() {
                const auto& candidate = candidates[index];
                try {
                    if (candidate.id % 5 == 0) {
                        sampler.abandon(candidate.id);
                    } else {
                        const auto ordinal = space.candidate_ordinal(candidate);
                        sampler.tell(candidate.id, -static_cast<double>(ordinal));
                    }
                } catch (const std::exception& error) {
                    terminal_errors[index] = error.what();
                }
            });
        }
        for (auto& thread : terminal_threads) {
            thread.join();
        }
        for (std::size_t index = 0; index < candidates.size(); ++index) {
            require(terminal_errors[index].empty(),
                    "finite concurrent terminal transition failed: " + terminal_errors[index]);
            if (candidates[index].id % 5 == 0) {
                ++abandoned;
            } else {
                ++told;
            }
        }
    }

    require(final_nonempty_batch == 6,
            "30 candidates with 8 workers did not end in a six-candidate partial batch");
    require(ids.size() == *cardinality && ordinals.size() == *cardinality,
            "finite concurrent lifecycle did not cover exact cardinality");
    for (std::uint64_t ordinal = 0; ordinal < *cardinality; ++ordinal) {
        require(ordinals.count(ordinal) == 1,
                "finite concurrent lifecycle left a gap in ordinal coverage");
    }
    require(told + abandoned == *cardinality,
            "finite concurrent lifecycle lost a terminal transition");
    require(sampler.generated() == *cardinality && sampler.completed() == told &&
                sampler.outstanding() == 0,
            "finite concurrent lifecycle counters are incorrect at exhaustion");
    require(!sampler.ask().has_value(),
            "finite concurrent lifecycle produced a candidate after exhaustion");
}

}  // namespace

int main() {
    try {
        test_concurrent_lifecycle();
        test_concurrent_reset_rejection_preserves_pending();
        test_concurrent_finite_exhaustion_with_partial_batch();
        std::cout << "TPE concurrency tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "TPE concurrency test failure: " << error.what() << '\n';
        return 1;
    }
}
