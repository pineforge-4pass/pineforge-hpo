#pragma once

#include <pineforge/hpo/error.hpp>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <memory>
#include <ostream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>

namespace pineforge::hpo::detail {

// The ordered writer is the sole producer. seal() and copy_array() run only after it has
// drained (or on that same writer's timeout path). Neither rows nor the JSON array are
// accumulated in memory: the only copy buffer has a fixed size.
class TrialSpool final {
public:
    TrialSpool(const std::filesystem::path& trials_file, const std::filesystem::path& output)
        : file_(open(trials_file, output)), output_(output) {}

    void append(const std::string& line) {
        if (line.empty())  // A proposed trial that never started has no terminal row.
            return;
        if (failed_ || sealed_ || line.back() != '\n' ||
            line.size() > std::numeric_limits<std::uint64_t>::max() - bytes_)
            fail("invalid terminal trial spool write");
        if (std::fwrite(line.data(), 1, line.size(), file_.get()) != line.size() ||
            std::fflush(file_.get()) != 0) {
            failed_ = true;
            fail("failed writing terminal trial spool");
        }
        bytes_ += line.size();
        ++rows_;
    }

    void seal(std::uint64_t terminal_trials) {
        if (failed_ || rows_ != terminal_trials)
            fail("terminal trial spool is incomplete");
        struct stat status {};
        if (std::fflush(file_.get()) != 0 || ::fstat(::fileno(file_.get()), &status) != 0 ||
            status.st_size < 0 || static_cast<std::uint64_t>(status.st_size) != bytes_)
            fail("cannot finalize terminal trial spool");
        // Previously --output could overwrite --trials-file (including an inode alias).
        // Preserve that publication behavior without truncating our only source of rows.
        struct stat destination {};
        if (!output_.empty()) {
            if (::stat(output_.c_str(), &destination) == 0) {
                if (status.st_dev == destination.st_dev && status.st_ino == destination.st_ino)
                    preserve_before_overwrite();
            } else if (errno != ENOENT && errno != ENOTDIR) {
                fail("cannot inspect output file before terminal trial spool copy");
            }
        }
        sealed_ = true;
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
        if (!sealed_ || !file_ || ::fseeko(file_.get(), 0, SEEK_SET) != 0)
            fail("cannot rewind terminal trial spool");
        std::array<char, 16384> buffer{};
        std::uint64_t copied_bytes = 0;
        std::uint64_t copied_rows = 0;
        bool row_start = true;
        for (;;) {
            const auto count = std::fread(buffer.data(), 1, buffer.size(), file_.get());
            if (std::ferror(file_.get()))
                fail("failed reading terminal trial spool");
            if (count == 0)
                break;
            copied_bytes += count;
            auto begin = buffer.data();
            const auto end = begin + count;
            while (begin != end) {
                if (row_start) {
                    output << "    ";
                    row_start = false;
                }
                const auto newline = std::find(begin, end, '\n');
                output.write(begin, newline - begin);
                begin = newline;
                if (begin != end) {
                    ++copied_rows;
                    output << (copied_rows < rows_ ? ",\n" : "\n");
                    row_start = true;
                    ++begin;
                }
            }
            if (!output)
                fail("failed copying terminal trial spool to result JSON");
        }
        if (!row_start || copied_rows != rows_ || copied_bytes != bytes_)
            fail("terminal trial spool changed or is incomplete");
    }

    void close() {
        auto* file = file_.release();
        if (file && std::fclose(file) != 0)
            fail("failed closing terminal trial spool");
    }

private:
    struct CloseFile {
        void operator()(std::FILE* file) const noexcept { std::fclose(file); }
    };
    using File = std::unique_ptr<std::FILE, CloseFile>;

    [[noreturn]] static void fail(const char* message) {
        throw TypedHpoError<std::runtime_error>("hpo_output_io_failed", {}, message);
    }

    void preserve_before_overwrite() {
        auto replacement = open({}, output_);
        if (::fseeko(file_.get(), 0, SEEK_SET) != 0)
            fail("cannot rewind terminal trial spool before output overwrite");
        std::array<char, 16384> buffer{};
        std::uint64_t copied = 0;
        for (;;) {
            const auto count = std::fread(buffer.data(), 1, buffer.size(), file_.get());
            if (std::ferror(file_.get()))
                fail("cannot read terminal trial spool before output overwrite");
            if (count == 0)
                break;
            if (std::fwrite(buffer.data(), 1, count, replacement.get()) != count)
                fail("cannot preserve terminal trial spool before output overwrite");
            copied += count;
        }
        if (copied != bytes_ || std::fflush(replacement.get()) != 0)
            fail("cannot finalize terminal trial spool before output overwrite");
        close();
        file_ = std::move(replacement);
    }

    static File open(const std::filesystem::path& trials_file,
                     const std::filesystem::path& output) {
        if (!trials_file.empty()) {
            File file(std::fopen(trials_file.c_str(), "w+b"));
            if (!file)
                fail("cannot open --trials-file as terminal trial spool");
            return file;
        }
        std::filesystem::path directory;
        if (!output.empty()) {
            directory = output.parent_path();
            if (directory.empty())
                directory = ".";
        } else {
            const auto* temporary_directory = std::getenv("TMPDIR");
            directory = temporary_directory && *temporary_directory ? temporary_directory : "/tmp";
        }
        auto pattern = (directory / ".pineforge-hpo-trials-XXXXXX").string();
        const auto descriptor = ::mkstemp(pattern.data());
        if (descriptor < 0)
            fail("cannot create terminal trial spool");
        // The program owns this newly created file; no named temporary survives the run.
        if (::unlink(pattern.c_str()) != 0) {
            ::close(descriptor);
            fail("cannot unlink terminal trial spool");
        }
        File file(::fdopen(descriptor, "w+b"));
        if (!file) {
            ::close(descriptor);
            fail("cannot open terminal trial spool stream");
        }
        return file;
    }

    File file_;
    std::filesystem::path output_;
    std::uint64_t bytes_ = 0;
    std::uint64_t rows_ = 0;
    bool failed_ = false;
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
