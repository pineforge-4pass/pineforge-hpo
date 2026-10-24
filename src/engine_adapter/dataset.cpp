#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>

#include "dataset_bin.hpp"
#include "../core/json.hpp"
#include "../core/sha256.hpp"

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/mman.h>
#include <sys/stat.h>
#include <type_traits>
#include <unordered_map>
#include <unistd.h>
#include <utility>
#include <vector>

namespace pineforge {
namespace hpo {
namespace {

static_assert(std::is_standard_layout<pf_bar_t>::value &&
                  std::is_trivially_copyable<pf_bar_t>::value,
              "pf_bar_t/v1 requires a trivial, standard-layout bar");
static_assert(sizeof(pf_bar_t) == 48, "pf_bar_t/v1 records must occupy 48 bytes");
static_assert(sizeof(double) == 8 && std::numeric_limits<double>::is_iec559,
              "pf_bar_t/v1 requires IEEE-754 binary64 prices");
static_assert(offsetof(pf_bar_t, open) == 0 && offsetof(pf_bar_t, high) == 8 &&
                  offsetof(pf_bar_t, low) == 16 && offsetof(pf_bar_t, close) == 24 &&
                  offsetof(pf_bar_t, volume) == 32 && offsetof(pf_bar_t, timestamp) == 40,
              "pf_bar_t/v1 field offsets must match <5dq");
static_assert(std::is_same<decltype(pf_bar_t::timestamp), std::int64_t>::value,
              "pf_bar_t/v1 timestamps must be signed int64 Unix milliseconds");

std::string trim(std::string value) {
    const auto first = std::find_if_not(value.begin(), value.end(), [](unsigned char ch) {
        return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
    });
    const auto last = std::find_if_not(value.rbegin(), value.rend(), [](unsigned char ch) {
                          return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
                      }).base();
    if (first >= last) {
        return {};
    }
    return std::string(first, last);
}

std::string normalize_header(std::string value, bool first_column) {
    value = trim(std::move(value));
    if (first_column && value.size() >= 3 && static_cast<unsigned char>(value[0]) == 0xEF &&
        static_cast<unsigned char>(value[1]) == 0xBB &&
        static_cast<unsigned char>(value[2]) == 0xBF) {
        value.erase(0, 3);
    }
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        if (ch >= 'A' && ch <= 'Z') {
            return static_cast<char>(ch - 'A' + 'a');
        }
        return static_cast<char>(ch);
    });
    return value;
}

std::vector<std::string> parse_csv_row(const std::string& line, std::size_t line_number) {
    std::vector<std::string> values;
    std::string value;
    bool in_quotes = false;
    bool after_quote = false;

    for (std::size_t index = 0; index < line.size(); ++index) {
        const char ch = line[index];
        if (in_quotes) {
            if (ch == '"') {
                if (index + 1 < line.size() && line[index + 1] == '"') {
                    value.push_back('"');
                    ++index;
                } else {
                    in_quotes = false;
                    after_quote = true;
                }
            } else {
                value.push_back(ch);
            }
            continue;
        }

        if (after_quote) {
            if (ch == ',') {
                values.push_back(trim(std::move(value)));
                value.clear();
                after_quote = false;
            } else if (ch != ' ' && ch != '\t' && ch != '\r') {
                throw TypedHpoError<std::runtime_error>(
                    "hpo_dataset_invalid", {},
                    "invalid character after quoted CSV field on line " +
                        std::to_string(line_number));
            }
            continue;
        }

        if (ch == ',') {
            values.push_back(trim(std::move(value)));
            value.clear();
        } else if (ch == '"') {
            if (!trim(value).empty()) {
                throw TypedHpoError<std::runtime_error>(
                    "hpo_dataset_invalid", {},
                    "quote inside unquoted CSV field on line " + std::to_string(line_number));
            }
            value.clear();
            in_quotes = true;
        } else {
            value.push_back(ch);
        }
    }

    if (in_quotes) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_dataset_invalid", {},
            "unterminated quoted CSV field on line " + std::to_string(line_number));
    }
    values.push_back(trim(std::move(value)));
    return values;
}

double parse_finite_double(const std::string& text, const char* column, std::size_t line_number) {
    std::istringstream stream(text);
    stream.imbue(std::locale::classic());
    double result = 0.0;
    if (!(stream >> result)) {
        throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {},
                                                "invalid " + std::string(column) +
                                                    " value on line " +
                                                    std::to_string(line_number) + ": " + text);
    }
    stream >> std::ws;
    if (stream.peek() != std::char_traits<char>::eof() || !std::isfinite(result)) {
        throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {},
                                                "invalid " + std::string(column) +
                                                    " value on line " +
                                                    std::to_string(line_number) + ": " + text);
    }
    return result;
}

std::int64_t parse_timestamp(const std::string& text, std::size_t line_number) {
    std::int64_t result = 0;
    const char* const begin = text.data();
    const char* const end = begin + text.size();
    const auto parsed = std::from_chars(begin, end, result, 10);
    if (parsed.ec != std::errc{} || parsed.ptr != end) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_dataset_invalid", {},
            "invalid timestamp value on line " + std::to_string(line_number) + ": " + text);
    }
    return result;
}

bool blank_line(const std::string& line) {
    return std::all_of(line.begin(), line.end(), [](unsigned char ch) {
        return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
    });
}

}  // namespace

Dataset::Dataset(std::vector<pf_bar_t> bars) : bars_(std::move(bars)) {
    for (std::size_t index = 0; index < bars_.size(); ++index) {
        const pf_bar_t& bar = bars_[index];
        if (!std::isfinite(bar.open) || !std::isfinite(bar.high) || !std::isfinite(bar.low) ||
            !std::isfinite(bar.close) || !std::isfinite(bar.volume)) {
            throw TypedHpoError<std::invalid_argument>(
                "hpo_dataset_invalid", {},
                "dataset contains a non-finite OHLCV value at index " + std::to_string(index));
        }
        if (index > 0 && bar.timestamp <= bars_[index - 1].timestamp) {
            throw TypedHpoError<std::invalid_argument>(
                "hpo_dataset_invalid", {},
                "dataset timestamps must be strictly increasing at index " + std::to_string(index));
        }
    }
}

Dataset Dataset::load_csv(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {},
                                                "cannot open OHLCV CSV: " + path.string());
    }

    std::string header_line;
    if (!std::getline(input, header_line)) {
        throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {},
                                                "OHLCV CSV is empty: " + path.string());
    }

    const std::vector<std::string> raw_headers = parse_csv_row(header_line, 1);
    std::unordered_map<std::string, std::size_t> columns;
    columns.reserve(raw_headers.size());
    for (std::size_t index = 0; index < raw_headers.size(); ++index) {
        const std::string name = normalize_header(raw_headers[index], index == 0);
        if (name.empty()) {
            throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {},
                                                    "empty CSV header column in " + path.string());
        }
        if (!columns.emplace(name, index).second) {
            throw TypedHpoError<std::runtime_error>(
                "hpo_dataset_invalid", {},
                "duplicate CSV header column '" + name + "' in " + path.string());
        }
    }

    const char* const required[] = {"timestamp", "open", "high", "low", "close", "volume"};
    for (const char* name : required) {
        if (columns.find(name) == columns.end()) {
            throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {},
                                                    "OHLCV CSV is missing required column '" +
                                                        std::string(name) + "': " + path.string());
        }
    }

    std::vector<pf_bar_t> bars;
    std::string line;
    std::size_t line_number = 1;
    while (std::getline(input, line)) {
        ++line_number;
        if (blank_line(line)) {
            continue;
        }
        const std::vector<std::string> row = parse_csv_row(line, line_number);
        const auto field = [&](const char* name) -> const std::string& {
            const std::size_t index = columns.at(name);
            if (index >= row.size()) {
                throw TypedHpoError<std::runtime_error>(
                    "hpo_dataset_invalid", {},
                    "CSV row has too few columns on line " + std::to_string(line_number));
            }
            return row[index];
        };

        pf_bar_t bar{};
        bar.timestamp = parse_timestamp(field("timestamp"), line_number);
        bar.open = parse_finite_double(field("open"), "open", line_number);
        bar.high = parse_finite_double(field("high"), "high", line_number);
        bar.low = parse_finite_double(field("low"), "low", line_number);
        bar.close = parse_finite_double(field("close"), "close", line_number);
        bar.volume = parse_finite_double(field("volume"), "volume", line_number);
        bars.push_back(bar);
    }

    if (bars.empty()) {
        throw TypedHpoError<std::runtime_error>(
            "hpo_dataset_invalid", {}, "OHLCV CSV contains no data rows: " + path.string());
    }
    return Dataset(std::move(bars));
}

namespace {

[[noreturn]] void binary_dataset_error(const std::string& cause) {
    throw TypedHpoError<std::runtime_error>("hpo_dataset_invalid", {}, "OHLCV binary: " + cause);
}

class BinaryDescriptor final {
public:
    explicit BinaryDescriptor(const std::filesystem::path& path, bool sidecar = false)
        : descriptor_(::open(path.c_str(), O_RDONLY | O_NONBLOCK)) {
        if (descriptor_ < 0) {
            if (sidecar) {
                binary_dataset_error(std::string(errno == ENOENT ? "sidecar missing: " :
                                                                  "sidecar unreadable: ") +
                                     path.string());
            }
            binary_dataset_error("cannot open OHLCV binary: " + path.string() + ": " +
                                 std::strerror(errno));
        }
    }

    ~BinaryDescriptor() { ::close(descriptor_); }
    BinaryDescriptor(const BinaryDescriptor&) = delete;
    BinaryDescriptor& operator=(const BinaryDescriptor&) = delete;
    int get() const noexcept { return descriptor_; }

private:
    int descriptor_;
};

struct stat binary_file_status(int descriptor, const std::filesystem::path& path) {
    struct stat info {};
    if (::fstat(descriptor, &info) != 0) {
        binary_dataset_error("cannot stat OHLCV binary: " + path.string() + ": " +
                             std::strerror(errno));
    }
    if (!S_ISREG(info.st_mode)) {
        binary_dataset_error("not a regular file: " + path.string());
    }
    return info;
}

bool same_timestamp(const struct timespec& left, const struct timespec& right) noexcept {
    return left.tv_sec == right.tv_sec && left.tv_nsec == right.tv_nsec;
}

bool same_modification_time(const struct stat& left, const struct stat& right) noexcept {
#if defined(__APPLE__)
    return same_timestamp(left.st_mtimespec, right.st_mtimespec);
#else
    return same_timestamp(left.st_mtim, right.st_mtim);
#endif
}

bool same_change_time(const struct stat& left, const struct stat& right) noexcept {
#if defined(__APPLE__)
    return same_timestamp(left.st_ctimespec, right.st_ctimespec);
#else
    return same_timestamp(left.st_ctim, right.st_ctim);
#endif
}

bool sidecar_integer_equals(const detail::Json* value, std::int64_t expected) {
    if (!value || value->kind != detail::Json::Kind::Number) {
        return false;
    }
    std::int64_t integer = 0;
    const auto* begin = value->value.data();
    const auto* end = begin + value->value.size();
    const auto parsed = std::from_chars(begin, end, integer, 10);
    return parsed.ec == std::errc{} && parsed.ptr == end && integer == expected;
}

bool canonical_sidecar_timeframe(const detail::Json* value) {
    if (!value || value->kind != detail::Json::Kind::String || value->value.empty()) {
        return false;
    }
    // Full-match the engine's [1-9][0-9]{0,4}|[1-9][0-9]{0,3}[DWMS] without regex scratch.
    const std::string_view text(value->value);
    const bool unit = text.back() == 'D' || text.back() == 'W' ||
                      text.back() == 'M' || text.back() == 'S';
    const auto digits = text.size() - (unit ? 1 : 0);
    return digits >= 1 && digits <= (unit ? 4U : 5U) && text.front() >= '1' &&
           text.front() <= '9' && std::all_of(text.begin(), text.begin() + digits,
                                            [](char ch) { return ch >= '0' && ch <= '9'; });
}

std::string check_binary_sidecar(const std::filesystem::path& path, const pf_bar_t* bars,
                                 std::size_t count) {
    const std::filesystem::path sidecar(path.string() + ".json");
    // Open nonblocking, then fstat BEFORE any read/stream open: FIFOs cannot hang, and directory
    // refusals do not depend on the standard library's ifstream/EISDIR behavior.
    const BinaryDescriptor input(sidecar, true);
    struct stat info {};
    if (::fstat(input.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_size < 0) {
        binary_dataset_error("sidecar unreadable: " + sidecar.string());
    }
    // Allocate the text once, bounded independently of bar count. The parsed tree is bounded by
    // the same 1 MiB input limit. No bar bytes are copied into this storage.
    constexpr std::size_t sidecar_limit = 1024 * 1024;
    if (static_cast<std::uint64_t>(info.st_size) > sidecar_limit) {
        binary_dataset_error("sidecar invalid JSON: exceeds 1048576 bytes: " + sidecar.string());
    }
    std::string text(static_cast<std::size_t>(info.st_size), '\0');
    std::size_t consumed = 0;
    while (consumed < text.size()) {
        const auto bytes = ::read(input.get(), text.data() + consumed, text.size() - consumed);
        if (bytes < 0 && errno == EINTR) {
            continue;
        }
        if (bytes <= 0) {
            binary_dataset_error("sidecar unreadable: " + sidecar.string());
        }
        consumed += static_cast<std::size_t>(bytes);
    }
    char extra;
    ssize_t trailing;
    do {
        trailing = ::read(input.get(), &extra, 1);
    } while (trailing < 0 && errno == EINTR);
    if (trailing != 0) {
        binary_dataset_error("sidecar unreadable: " + sidecar.string());
    }
    detail::Json document;
    try {
        document = detail::parse_json(text, sidecar_limit);
    } catch (const HpoError& error) {
        binary_dataset_error("sidecar invalid JSON: " + sidecar.string() + ": " + error.what());
    }
    if (document.kind != detail::Json::Kind::Object) {
        binary_dataset_error("sidecar must be a JSON object: " + sidecar.string());
    }
    const auto* format = document.find("format");
    const auto* symbol = document.find("symbol");
    const auto* digest = document.find("sha256");
    if (!format || format->kind != detail::Json::Kind::String || format->value != "pf_bar_t/v1" ||
        !sidecar_integer_equals(document.find("record_size"), 48) ||
        !symbol || symbol->kind != detail::Json::Kind::String ||
        !canonical_sidecar_timeframe(document.find("tf")) ||
        !sidecar_integer_equals(document.find("count"), static_cast<std::int64_t>(count)) ||
        !sidecar_integer_equals(document.find("first_ts"), bars[0].timestamp) ||
        !sidecar_integer_equals(document.find("last_ts"), bars[count - 1].timestamp) ||
        !digest || digest->kind != detail::Json::Kind::String || digest->value.size() != 64 ||
        !std::all_of(digest->value.begin(), digest->value.end(), [](char ch) {
            return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
        })) {
        binary_dataset_error("sidecar does not match the bar/close files");
    }
    return digest->value;
}

void check_binary_records(const pf_bar_t* bars, std::size_t count) {
    for (std::size_t index = 0; index < count; ++index) {
        const auto& bar = bars[index];
        if (!std::isfinite(bar.open) || !std::isfinite(bar.high) || !std::isfinite(bar.low) ||
            !std::isfinite(bar.close)) {
            binary_dataset_error("non-finite OHLC value at record " + std::to_string(index));
        }
        if (bar.volume < 0 || std::isinf(bar.volume)) {
            binary_dataset_error("volume must be nonnegative or NaN at record " +
                                 std::to_string(index));
        }
        if (index > 0 && bar.timestamp <= bars[index - 1].timestamp) {
            binary_dataset_error("timestamps must be strictly increasing at record " +
                                 std::to_string(index));
        }
    }
}

}  // namespace

struct Dataset::MappedFile final {
    explicit MappedFile(const std::filesystem::path& path)
        : path(path), descriptor(path), identity(binary_file_status(descriptor.get(), path)) {}

    void verify_unchanged(const pf_bar_t* mapped_bars, std::size_t length) const {
        struct stat current {};
        if (::fstat(descriptor.get(), &current) != 0) {
            binary_dataset_error("changed while mapped (cannot stat descriptor): " +
                                 path.string());
        }
        detail::require_bin_unchanged(current, identity, mapped_bars, length, sidecar_digest, path);
    }

    const std::filesystem::path path;
    const BinaryDescriptor descriptor;
    const struct stat identity;
    std::string sidecar_digest;
};

void detail::require_bin_little_endian(bool little_endian) {
    if (!little_endian) {
        binary_dataset_error("big-endian host is unsupported; pf_bar_t/v1 requires little-endian");
    }
}

void detail::require_bin_address_space(std::uint64_t bytes, std::uint64_t maximum,
                                       const std::filesystem::path& path) {
    if (bytes > maximum) {
        binary_dataset_error("size exceeds host address space: " + path.string());
    }
}

void detail::require_bin_unchanged(const struct stat& current, const struct stat& identity,
                                   const pf_bar_t* mapped_bars, std::size_t length,
                                   std::string_view sidecar_digest,
                                   const std::filesystem::path& path) {
    if (current.st_dev != identity.st_dev || current.st_ino != identity.st_ino ||
        current.st_size != identity.st_size || !same_modification_time(current, identity)) {
        binary_dataset_error("changed while mapped: " + path.string());
    }
    // Accept unchanged identity, or only metadata moved and the mapped bytes still hash to
    // the sidecar digest. Use the held mapping, never the current path's contents.
    // MUTANT: the ctime branch (and with it the re-hash) is skipped, ctime-only moves pass.
    if (false && !same_change_time(current, identity) &&
        (sidecar_digest.empty() ||
         sidecar_digest != detail::sha256(std::string_view(
             reinterpret_cast<const char*>(mapped_bars), length)))) {
        binary_dataset_error("changed while mapped: " + path.string());
    }
}

std::uint64_t detail::binary_file_size(int descriptor, const std::filesystem::path& path) {
    return static_cast<std::uint64_t>(binary_file_status(descriptor, path).st_size);
}

std::shared_ptr<const pf_bar_t> detail::map_bin_records(
    int descriptor, std::size_t length, const std::filesystem::path& path) {
    void* const address = ::mmap(nullptr, length, PROT_READ, MAP_PRIVATE, descriptor, 0);
    if (address == MAP_FAILED) {
        binary_dataset_error("cannot mmap OHLCV binary: " + path.string() + ": " +
                             std::strerror(errno));
    }
    return std::shared_ptr<const pf_bar_t>(static_cast<const pf_bar_t*>(address),
        [length](const pf_bar_t* bars) {
            ::munmap(const_cast<pf_bar_t*>(bars), length);
        });
}

Dataset::Dataset(std::shared_ptr<const MappedFile> mapped_file,
                 std::shared_ptr<const pf_bar_t> mapped_bars, std::size_t count)
    : mapped_file_(std::move(mapped_file)), mapped_bars_(std::move(mapped_bars)),
      mapped_count_(count) {}

const std::vector<pf_bar_t>& Dataset::bars() const {
    if (mapped_bars_) {
        throw std::logic_error(
            "bars() is not available for a mapped dataset; use data() and size()");
    }
    return bars_;
}

void Dataset::verify_unchanged() const {
    if (mapped_file_) {
        mapped_file_->verify_unchanged(mapped_bars_.get(), mapped_count_ * sizeof(pf_bar_t));
    }
}

Dataset Dataset::load_bin(const std::filesystem::path& path, BinaryVerification verification) {
    const std::uint16_t endian_probe = 1;
    detail::require_bin_little_endian(
        *reinterpret_cast<const unsigned char*>(&endian_probe) == 1);
    const auto file = std::make_shared<MappedFile>(path);
    const auto bytes = static_cast<std::uint64_t>(file->identity.st_size);
    if (bytes % 48 != 0) {
        binary_dataset_error("size is not a multiple of 48: " + path.string());
    }
    const std::uint64_t count = bytes / 48;
    if (count == 0) {
        binary_dataset_error("zero records: " + path.string());
    }
    if (count > 2'147'483'647) {
        binary_dataset_error("record count exceeds 2147483647: " + path.string());
    }
    detail::require_bin_address_space(bytes, std::numeric_limits<std::size_t>::max(), path);
    const auto length = static_cast<std::size_t>(bytes);
    auto mapped = detail::map_bin_records(file->descriptor.get(), length, path);
    file->sidecar_digest =
        check_binary_sidecar(path, mapped.get(), static_cast<std::size_t>(count));
    check_binary_records(mapped.get(), static_cast<std::size_t>(count));
    if (verification == BinaryVerification::Full &&
        file->sidecar_digest != detail::sha256(
            std::string_view(reinterpret_cast<const char*>(mapped.get()), length))) {
        binary_dataset_error("sidecar does not match the bar/close files");
    }
    Dataset dataset(file, std::move(mapped), static_cast<std::size_t>(count));
    dataset.verify_unchanged();
    return dataset;
}

}  // namespace hpo
}  // namespace pineforge
