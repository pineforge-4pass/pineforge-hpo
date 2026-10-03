#include "batch_executor.hpp"

#include <atomic>
#include <chrono>
#include <future>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace pfh = pineforge::hpo;

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

void test_coordinator_unwind(const std::string& phase) {
    std::promise<void> active;
    auto active_future = active.get_future();
    std::promise<void> release;
    auto release_future = release.get_future().share();
    std::promise<std::vector<std::future<int>>> queued;
    auto queued_future = queued.get_future();
    std::atomic<int> unstarted_executions{0};
    bool caught = false;
    std::thread coordinator([&] {
        try {
            pfh::BatchExecutor<int> workers(1);
            auto running = workers.submit([&] {
                active.set_value();
                release_future.wait();
                return 1;
            });
            active_future.wait();
            std::vector<std::future<int>> pending;
            for (unsigned index = 0; index < 8; ++index) {
                pending.push_back(workers.submit([&] {
                    ++unstarted_executions;
                    return 2;
                }));
            }
            queued.set_value(std::move(pending));
            throw std::runtime_error(phase);
        } catch (const std::runtime_error& error) {
            caught = error.what() == phase;
        }
    });
    auto pending = queued_future.get();
    const bool discarded_before_join =
        pending.front().wait_for(std::chrono::seconds(2)) == std::future_status::ready;
    release.set_value();
    coordinator.join();
    require(caught, "coordinator exception was not propagated");
    require(discarded_before_join, "queued work was retained during coordinator unwind");
    require(unstarted_executions == 0, "unstarted trials ran after coordinator exception");
    for (auto& result : pending) {
        bool abandoned = false;
        try {
            (void)result.get();
        } catch (const std::future_error& error) {
            abandoned = error.code() == std::make_error_code(std::future_errc::broken_promise);
        }
        require(abandoned, "discarded task did not release its future");
    }
}

int main() {
    test_coordinator_unwind("ask failed");
    test_coordinator_unwind("tell failed");
    pfh::BatchExecutor<int> workers(2);
    auto completed = workers.submit([] { return 42; });
    require(completed.get() == 42, "ordinary completed work changed");
    workers.close();
    return 0;
}
