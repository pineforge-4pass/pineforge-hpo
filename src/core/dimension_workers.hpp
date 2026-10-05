#pragma once

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <functional>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#ifdef __linux__
#include <sched.h>
#endif

namespace pineforge::hpo::detail {

inline unsigned quota_cpus(const std::string& quota, std::uint64_t period) {
    if (quota == "max" || quota == "-1" || !period)
        return 0;
    std::istringstream input(quota);
    std::uint64_t amount;
    if (!(input >> amount) || !amount)
        return 0;
    return static_cast<unsigned>(std::min<std::uint64_t>(
        1024, std::max<std::uint64_t>(1, amount / period)));
}

inline unsigned available_cpus() {
    auto count = std::max(1U, std::thread::hardware_concurrency());
#ifdef __linux__
    cpu_set_t affinity;
    CPU_ZERO(&affinity);
    if (sched_getaffinity(0, sizeof(affinity), &affinity) == 0)
        count = std::min(count, static_cast<unsigned>(std::max(1, CPU_COUNT(&affinity))));
    const auto inspect = [&](const std::filesystem::path& root, const std::string& relative,
                             bool unified) {
        auto directory = root / std::filesystem::path(relative).relative_path();
        while (true) {
            std::string quota;
            std::uint64_t period = 0;
            if (unified) {
                std::ifstream input(directory / "cpu.max");
                input >> quota >> period;
            } else {
                std::ifstream input(directory / "cpu.cfs_quota_us");
                std::ifstream interval(directory / "cpu.cfs_period_us");
                input >> quota;
                interval >> period;
            }
            const auto limit = quota_cpus(quota, period);
            if (limit)
                count = std::min(count, limit);
            if (directory == root || directory.empty() || directory == directory.parent_path())
                break;
            directory = directory.parent_path();
        }
    };
    inspect("/sys/fs/cgroup", "", true);
    std::ifstream groups("/proc/self/cgroup");
    std::string line;
    while (std::getline(groups, line)) {
        const auto first = line.find(':');
        const auto second = line.find(':', first + 1);
        if (first == std::string::npos || second == std::string::npos)
            continue;
        const auto controllers = ',' + line.substr(first + 1, second - first - 1) + ',';
        if (controllers == ",,")
            inspect("/sys/fs/cgroup", line.substr(second + 1), true);
        else if (controllers.find(",cpu,") != std::string::npos) {
            inspect("/sys/fs/cgroup/cpu", line.substr(second + 1), false);
            inspect("/sys/fs/cgroup/cpu,cpuacct", line.substr(second + 1), false);
        }
    }
#endif
    return count;
}

class DimensionWorkers {
public:
    using ThreadFactory = std::function<std::thread(std::function<void()>)>;

    explicit DimensionWorkers(ThreadFactory factory = [](std::function<void()> function) {
        return std::thread(std::move(function));
    }) : factory_(std::move(factory)) {}

    ~DimensionWorkers() { stop(); }

    void run(std::size_t dimensions, unsigned requested,
             const std::function<void(std::size_t)>& function) {
        if (!initialized_) {
            initialized_ = true;
            const auto count = std::min<std::size_t>(requested, dimensions);
            try {
                workers_.reserve(count > 0 ? count - 1 : 0);
                for (std::size_t index = 1; index < count; ++index)
                    workers_.push_back(factory_([this] { worker(); }));
            } catch (const std::system_error&) {
                stop();
                workers_.clear();
            } catch (const std::bad_alloc&) {
                stop();
                workers_.clear();
            }
        }
        if (workers_.empty()) {
            for (std::size_t index = 0; index < dimensions; ++index)
                function(index);
            return;
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            function_ = function;
            dimensions_ = dimensions;
            next_.store(0, std::memory_order_relaxed);
            remaining_ = workers_.size();
            error_ = nullptr;
            ++generation_;
        }
        ready_.notify_all();
        execute();
        std::unique_lock<std::mutex> lock(mutex_);
        done_.wait(lock, [&] { return remaining_ == 0; });
        function_ = {};
        if (error_)
            std::rethrow_exception(error_);
    }

private:
    void execute() {
        try {
            while (true) {
                const auto index = next_.fetch_add(1, std::memory_order_relaxed);
                if (index >= dimensions_)
                    return;
                function_(index);
            }
        } catch (...) {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!error_)
                error_ = std::current_exception();
        }
    }

    void worker() {
        std::uint64_t observed = 0;
        while (true) {
            std::unique_lock<std::mutex> lock(mutex_);
            ready_.wait(lock, [&] { return stopped_ || generation_ != observed; });
            if (stopped_)
                return;
            observed = generation_;
            lock.unlock();
            execute();
            lock.lock();
            if (--remaining_ == 0)
                done_.notify_one();
        }
    }

    void stop() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopped_ = true;
        }
        ready_.notify_all();
        for (auto& worker : workers_)
            if (worker.joinable())
                worker.join();
    }

    ThreadFactory factory_;
    std::vector<std::thread> workers_;
    std::mutex mutex_;
    std::condition_variable ready_, done_;
    std::function<void(std::size_t)> function_;
    std::atomic<std::size_t> next_{0};
    std::size_t dimensions_ = 0, remaining_ = 0;
    std::uint64_t generation_ = 0;
    std::exception_ptr error_;
    bool initialized_ = false, stopped_ = false;
};

}
