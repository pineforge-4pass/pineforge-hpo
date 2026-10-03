#pragma once

#include <cerrno>
#include <cstdio>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
#include <utility>
#include <unistd.h>

namespace pineforge::hpo::detail {

class OrdinalSet final {
public:
    bool contains(std::uint64_t value) const {
        return file_ && locate(file_.get(), capacity_, value).second;
    }

    bool insert(std::uint64_t value) {
        if (!file_)
            file_ = create(capacity_);
        if (contains(value))
            return false;
        if (size_ >= capacity_ / 2)
            grow();
        const auto index = locate(file_.get(), capacity_, value).first;
        write(file_.get(), index, Slot{value, 1});
        ++size_;
        return true;
    }

    void clear() {
        file_.reset();
        capacity_ = 1024;
        size_ = 0;
    }

    std::uint64_t size() const noexcept { return size_; }

private:
    struct Slot { std::uint64_t value = 0; std::uint64_t occupied = 0; };
    struct CloseFile {
        void operator()(std::FILE* file) const noexcept { std::fclose(file); }
    };
    using File = std::unique_ptr<std::FILE, CloseFile>;

    static File create(std::uint64_t capacity) {
        if (capacity > static_cast<std::uint64_t>(std::numeric_limits<off_t>::max()) / sizeof(Slot))
            throw std::overflow_error("finite coverage index is too large");
        File file(std::tmpfile());
        if (!file || ::ftruncate(::fileno(file.get()), capacity * sizeof(Slot)) != 0)
            throw std::runtime_error("cannot create finite coverage temporary index");
        return file;
    }

    static Slot read(std::FILE* file, std::uint64_t index) {
        Slot slot;
        ssize_t count;
        do {
            count = ::pread(::fileno(file), &slot, sizeof(slot), index * sizeof(slot));
        } while (count < 0 && errno == EINTR);
        if (count != sizeof(slot))
            throw std::runtime_error("cannot read finite coverage temporary index");
        return slot;
    }

    static void write(std::FILE* file, std::uint64_t index, const Slot& slot) {
        ssize_t count;
        do {
            count = ::pwrite(::fileno(file), &slot, sizeof(slot), index * sizeof(slot));
        } while (count < 0 && errno == EINTR);
        if (count != sizeof(slot))
            throw std::runtime_error("cannot write finite coverage temporary index");
    }

    static std::pair<std::uint64_t, bool> locate(std::FILE* file, std::uint64_t capacity,
                                               std::uint64_t value) {
        auto hash = value + 0x9e3779b97f4a7c15ULL;
        hash = (hash ^ (hash >> 30)) * 0xbf58476d1ce4e5b9ULL;
        hash = (hash ^ (hash >> 27)) * 0x94d049bb133111ebULL;
        hash ^= hash >> 31;
        for (auto index = hash & (capacity - 1);; index = (index + 1) & (capacity - 1)) {
            const auto slot = read(file, index);
            if (!slot.occupied || slot.value == value)
                return {index, slot.occupied != 0};
        }
    }

    void grow() {
        if (capacity_ > std::numeric_limits<std::uint64_t>::max() / 2)
            throw std::overflow_error("finite coverage index is too large");
        const auto next_capacity = capacity_ * 2;
        auto next = create(next_capacity);
        for (std::uint64_t index = 0; index < capacity_; ++index) {
            const auto slot = read(file_.get(), index);
            if (slot.occupied)
                write(next.get(), locate(next.get(), next_capacity, slot.value).first, slot);
        }
        file_ = std::move(next);
        capacity_ = next_capacity;
    }

    File file_;
    std::uint64_t capacity_ = 1024;
    std::uint64_t size_ = 0;
};

}  // namespace pineforge::hpo::detail
