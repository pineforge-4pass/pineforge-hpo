#include <pineforge/hpo/dataset.hpp>
#include <pineforge/hpo/error.hpp>

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <limits>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace pineforge {
namespace hpo {
namespace {

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

}  // namespace hpo
}  // namespace pineforge
