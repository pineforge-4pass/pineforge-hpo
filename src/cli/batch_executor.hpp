#pragma once

#include <pineforge/hpo/error.hpp>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <future>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

namespace pineforge::hpo {

template <typename Result>
class BatchExecutor final {
public:
    explicit BatchExecutor(unsigned count) : started_(Clock::now()) {
        if (count == 0)
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "sampler"}},
                "batch executor requires at least one worker");
        try {
            for (unsigned worker = 0; worker < count; ++worker) {
                workers_.emplace_back([this] {
                    for (;;) {
                        std::packaged_task<Result()> task;
                        {
                            std::unique_lock<std::mutex> lock(mutex_);
                            ready_.wait(lock, [&] { return closing_ || !queue_.empty(); });
                            if (queue_.empty())
                                return;
                            task = std::move(queue_.front());
                            queue_.pop_front();
                        }
                        const auto begin = Clock::now();
                        task();
                        busy_ns_.fetch_add(
                            std::chrono::duration_cast<std::chrono::nanoseconds>(
                                Clock::now() - begin).count(), std::memory_order_relaxed);
                    }
                });
            }
        } catch (...) {
            close();
            throw;
        }
    }

    ~BatchExecutor() { close(); }
    BatchExecutor(const BatchExecutor&) = delete;
    BatchExecutor& operator=(const BatchExecutor&) = delete;

    template <typename Function>
    std::future<Result> submit(Function&& function) {
        std::packaged_task<Result()> task(std::forward<Function>(function));
        auto future = task.get_future();
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (closing_)
                throw TypedHpoError<std::logic_error>("hpo_invariant", {},
                                                      "batch executor is closed");
            queue_.push_back(std::move(task));
        }
        ready_.notify_one();
        return future;
    }

    void close() noexcept {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            closing_ = true;
            queue_.clear();
        }
        ready_.notify_all();
        for (auto& worker : workers_) {
            if (worker.joinable())
                worker.join();
        }
    }

    double busy_seconds() const noexcept { return busy_ns_.load() / 1e9; }
    double elapsed_seconds() const noexcept {
        return std::chrono::duration<double>(Clock::now() - started_).count();
    }

private:
    using Clock = std::chrono::steady_clock;
    Clock::time_point started_;
    std::atomic<std::int64_t> busy_ns_{0};
    std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<std::packaged_task<Result()>> queue_;
    std::vector<std::thread> workers_;
    bool closing_ = false;
};

}  // namespace pineforge::hpo
