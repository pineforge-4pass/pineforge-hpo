#pragma once

#include <pineforge/hpo/error.hpp>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <ostream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>

namespace pineforge::hpo::detail {

// Completion appends hold RunState's mutex. The ordered stream never owns this spool. After
// workers drain (or the watchdog freezes completion), seal() validates both anonymous files
// before publication; copying uses only a fixed-size buffer.
class TrialSpool final {
public:
    TrialSpool(const std::filesystem::path& output, std::uint64_t first_trial_id)
        : directory_(select_directory(output)),
          data_(open(directory_, ".pineforge-hpo-trials-data-XXXXXX")),
          index_(open(directory_, ".pineforge-hpo-trials-index-XXXXXX")),
          first_trial_id_(first_trial_id) {}

    void append(std::uint64_t trial_id, const std::string& line) {
        if (sealed_ || line.size() < 2 || line.back() != '\n' ||
            line.size() > kMaxFileBytes - bytes_)
            fail("invalid terminal trial spool write");
        if (trial_id < first_trial_id_)
            fail("terminal trial spool ID is outside the index range");
        const auto rel = trial_id - first_trial_id_;
        if (rel >= kMaxSlots)
            fail("terminal trial spool ID is outside the index range");
        const auto slot_offset = rel * kSlotBytes;
        if (slot_offset < index_bytes_) {
            Slot previous{};
            read_exact(index_.get(), reinterpret_cast<char*>(previous.data()), kSlotBytes,
                       slot_offset);
            if (previous[0] != 0 || previous[1] != 0)
                fail("duplicate terminal trial spool ID");
        }
        write_exact(data_.get(), line.data(), line.size(), bytes_);
        const Slot slot{bytes_, static_cast<std::uint64_t>(line.size())};
        write_exact(index_.get(), reinterpret_cast<const char*>(slot.data()), kSlotBytes,
                    slot_offset);
        max_rel_ = std::max(max_rel_, rel);
        index_bytes_ = std::max(index_bytes_, slot_offset + kSlotBytes);
        bytes_ += line.size();
        ++rows_;
    }

    void seal(std::uint64_t terminal_trials) {
        if (failed_)
            io_failure(first_failure_);
        if (rows_ != terminal_trials || data_.get() < 0 || index_.get() < 0)
            fail("terminal trial spool is incomplete");
        if (!sealed_) {
            check_size(data_.get(), bytes_);
            check_size(index_.get(), index_bytes_);
            copy_rows(nullptr);
            sealed_ = true;
        }
    }

    std::uint64_t array_bytes() const {
        const auto commas = rows_ == 0 ? 0 : rows_ - 1;
        if (commas > std::numeric_limits<std::uint64_t>::max() - bytes_ ||
            rows_ > (std::numeric_limits<std::uint64_t>::max() - bytes_ - commas) / 4)
            fail("terminal trial array is too large");
        // NDJSON already includes each LF; the array adds four spaces/row and N-1 commas.
        return bytes_ + rows_ * 4 + commas;
    }

    void copy_array(std::ostream& output) const {
        if (failed_ || !sealed_ || data_.get() < 0 || index_.get() < 0)
            fail("terminal trial spool is not sealed");
        copy_rows(&output);
    }

    void close() noexcept {
        // Both anonymous files have been fully read before publication. A close error here
        // cannot lose result bytes and must not turn a published result into another document.
        data_.close();
        index_.close();
    }

private:
    static constexpr std::uint64_t kSlotBytes = 16;
    static constexpr std::uint64_t kMaxFileBytes =
        static_cast<std::uint64_t>(std::numeric_limits<off_t>::max());
    static constexpr std::uint64_t kMaxSlots = kMaxFileBytes / kSlotBytes;
    static constexpr std::size_t kBufferBytes = 16384;
    using Slot = std::array<std::uint64_t, 2>;
    static_assert(sizeof(Slot) == kSlotBytes, "trial spool slots must be exactly 16 bytes");

    class File final {
    public:
        explicit File(int descriptor) : descriptor_(descriptor) {}
        File(File&& other) noexcept : descriptor_(std::exchange(other.descriptor_, -1)) {}
        File(const File&) = delete;
        File& operator=(const File&) = delete;
        ~File() {
            if (descriptor_ >= 0)
                ::close(descriptor_);
        }

        int get() const { return descriptor_; }
        void close() noexcept {
            const auto descriptor = std::exchange(descriptor_, -1);
            if (descriptor >= 0)
                ::close(descriptor);
        }

    private:
        int descriptor_;
    };

    [[noreturn]] static void io_failure(const char* message) {
        throw TypedHpoError<std::runtime_error>("hpo_output_io_failed", {}, message);
    }

    [[noreturn]] void fail(const char* message) const {
        if (!failed_) {
            failed_ = true;
            first_failure_ = message;
        }
        io_failure(first_failure_);
    }

    void check_size(int descriptor, std::uint64_t bytes) const {
        struct stat status {};
        if (::fstat(descriptor, &status) != 0 || status.st_size < 0 ||
            static_cast<std::uint64_t>(status.st_size) != bytes)
            fail("cannot finalize terminal trial spool size");
    }

    void write_exact(int descriptor, const char* data, std::size_t size,
                     std::uint64_t offset) {
        while (size != 0) {
            const auto chunk = std::min(size, kBufferBytes);
            const auto count = ::pwrite(descriptor, data, chunk, static_cast<off_t>(offset));
            if (count < 0 && errno == EINTR)
                continue;
            if (count <= 0)
                fail("failed writing terminal trial spool");
            data += count;
            size -= static_cast<std::size_t>(count);
            offset += static_cast<std::uint64_t>(count);
        }
    }

    void read_exact(int descriptor, char* data, std::size_t size, std::uint64_t offset) const {
        while (size != 0) {
            const auto chunk = std::min(size, kBufferBytes);
            const auto count = ::pread(descriptor, data, chunk, static_cast<off_t>(offset));
            if (count < 0 && errno == EINTR)
                continue;
            if (count <= 0)
                fail("failed reading terminal trial spool");
            data += count;
            size -= static_cast<std::size_t>(count);
            offset += static_cast<std::uint64_t>(count);
        }
    }

    // A null destination is the seal-time read check, before any result header is written.
    void copy_rows(std::ostream* output) const {
        std::array<char, kBufferBytes> buffer{};
        std::uint64_t copied_bytes = 0;
        std::uint64_t copied_rows = 0;
        for (std::uint64_t rel = 0; rows_ != 0 && rel <= max_rel_; ++rel) {
            Slot slot{};
            read_exact(index_.get(), reinterpret_cast<char*>(slot.data()), kSlotBytes,
                       rel * kSlotBytes);
            if (slot[1] == 0) {
                if (slot[0] != 0)
                    fail("invalid empty terminal trial spool slot");
                continue;
            }
            if (slot[0] > bytes_ || slot[1] > bytes_ - slot[0] || slot[1] < 2 ||
                slot[1] > bytes_ - copied_bytes || copied_rows >= rows_)
                fail("invalid terminal trial spool row bounds");
            if (output)
                *output << "    ";
            auto offset = slot[0];
            auto remaining = slot[1];
            while (remaining != 0) {
                const auto count = static_cast<std::size_t>(
                    std::min<std::uint64_t>(remaining, buffer.size()));
                read_exact(data_.get(), buffer.data(), count, offset);
                const bool last = remaining == count;
                if (last && buffer[count - 1] != '\n')
                    fail("terminal trial spool row is incomplete");
                if (output)
                    output->write(buffer.data(), count - (last ? 1 : 0));
                offset += count;
                remaining -= count;
            }
            copied_bytes += slot[1];
            ++copied_rows;
            // Destination failures belong to write_result_file/publish_result. Keep validating
            // the spool; the caller checks the destination stream after copying and flushing.
            if (output)
                *output << (copied_rows < rows_ ? ",\n" : "\n");
        }
        if (copied_rows != rows_ || copied_bytes != bytes_)
            fail("terminal trial spool changed or is incomplete");
    }

    static std::filesystem::path select_directory(const std::filesystem::path& output) {
        if (!output.empty()) {
            auto directory = output.parent_path();
            if (directory.empty())
                directory = ".";
            struct stat status {};
            if (::stat(directory.c_str(), &status) == 0 && S_ISDIR(status.st_mode) &&
                ::access(directory.c_str(), W_OK) == 0)
                return directory;
        }
        const auto* temporary_directory = std::getenv("TMPDIR");
        // A non-empty TMPDIR is an explicit choice. Refuse a missing/unwritable directory
        // instead of hiding that misconfiguration by retrying elsewhere.
        return temporary_directory && *temporary_directory ? temporary_directory : "/tmp";
    }

    static File open(const std::filesystem::path& directory, const char* filename) {
        auto pattern = (directory / filename).string();
        File file(::mkstemp(pattern.data()));
        if (file.get() < 0)
            io_failure("cannot create terminal trial spool");
        if (::unlink(pattern.c_str()) != 0)
            io_failure("cannot unlink terminal trial spool");
        return file;
    }

    const std::filesystem::path directory_;
    File data_;
    File index_;
    const std::uint64_t first_trial_id_;
    std::uint64_t max_rel_ = 0;
    std::uint64_t bytes_ = 0;
    std::uint64_t index_bytes_ = 0;
    std::uint64_t rows_ = 0;
    mutable bool failed_ = false;
    // Every fail() argument is a string literal; retaining the first needs no allocation.
    mutable const char* first_failure_ = nullptr;
    bool sealed_ = false;
};

// Metadata remains small for the ordinary batches/random path. Full rolling replay logs and
// explicitly imported warm history retain their separate, existing storage contracts.
struct ResultDocument {
    std::string before_trials;
    std::string after_trials;
    const TrialSpool* trials = nullptr;

    std::uint64_t size() const {
        return before_trials.size() + after_trials.size() + (trials ? trials->array_bytes() : 0);
    }

    void write(std::ostream& output) const {
        output << before_trials;
        if (trials)
            trials->copy_array(output);
        output << after_trials;
    }
};

}  // namespace pineforge::hpo::detail
