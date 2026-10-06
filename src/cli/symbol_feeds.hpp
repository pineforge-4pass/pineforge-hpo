#pragma once

#include <pineforge/hpo/error.hpp>

#include "continuation.hpp"
#include <pineforge/hpo/symbol_feeds.hpp>

#include <array>
#include <cctype>
#include <cerrno>
#include <cmath>
#include <ctime>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <regex>
#include <set>

namespace pineforge::hpo::detail {

inline constexpr std::int64_t symbol_stamp_max = 9007199254740991LL;
inline constexpr std::size_t symbol_csv_field_max = 131072;
inline constexpr const char* symbol_feed_canonicalization = "pf-symbol-feed-barc-close-le-v1";

[[noreturn]] inline void symbol_feed_error(const std::string& message) {
    throw TypedHpoError<std::invalid_argument>(
        "hpo_study_spec_invalid", {{"reason", "symbol_feeds"}}, "--symbol-feeds: " + message);
}

inline std::size_t symbol_utf8_length(const std::string& value) {
    std::size_t count = 0;
    for (std::size_t offset = 0; offset < value.size(); ++count) {
        const auto first = static_cast<unsigned char>(value[offset++]);
        if (first < 128)
            continue;
        const unsigned tails = first >= 0xf0 ? 3 : first >= 0xe0 ? 2 : first >= 0xc2 ? 1 : 0;
        if (!tails || first > 0xf4)
            symbol_feed_error("not a UTF-8 CSV");
        unsigned point = first & ((1u << (6 - tails)) - 1);
        for (unsigned index = 0; index < tails; ++index) {
            if (offset == value.size())
                symbol_feed_error("not a UTF-8 CSV");
            const auto tail = static_cast<unsigned char>(value[offset++]);
            if ((tail & 0xc0) != 0x80)
                symbol_feed_error("not a UTF-8 CSV");
            point = (point << 6) | (tail & 63);
        }
        if (point < (tails == 1 ? 0x80u : tails == 2 ? 0x800u : 0x10000u) ||
            point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff))
            symbol_feed_error("not a UTF-8 CSV");
    }
    return count;
}

inline std::string symbol_text(const Json& value, const std::string& where) {
    std::size_t length = 0;
    try {
        length = symbol_utf8_length(value.value);
    } catch (const std::invalid_argument&) {
        symbol_feed_error(where + " must be a valid UTF-8 string");
    }
    if (value.kind != Json::Kind::String || value.value.empty() ||
        length > 256 ||
        std::any_of(value.value.begin(), value.value.end(), [](unsigned char character) {
            return character < 32;
        }))
        symbol_feed_error(where + " must be a non-empty string of at most 256 characters "
                          "without control characters, got " + dump_json(value).substr(0, 80));
    return value.value;
}

inline void symbol_csv_cell(const std::string& cell, const std::string& where) {
    std::size_t length = 0;
    try {
        length = symbol_utf8_length(cell);
    } catch (const std::invalid_argument&) {
        symbol_feed_error(where + ": not a UTF-8 CSV");
    }
    if (length > symbol_csv_field_max)
        symbol_feed_error(where + ": CSV field exceeds 131072 characters");
}

inline std::string symbol_timeframe(std::string timeframe, const std::string& where) {
    if (timeframe == "D" || timeframe == "W" || timeframe == "M" || timeframe == "S")
        timeframe = "1" + timeframe;
    static const std::regex spelling("(?:[1-9][0-9]{0,4}|[1-9][0-9]{0,3}[DWMS])");
    if (!std::regex_match(timeframe, spelling))
        symbol_feed_error(where + ": a timeframe is whole minutes (\"15\", \"240\") or "
                          "<n>D|W|M|S (\"1D\", \"1W\"), got " +
                          dump_json(Json::string(timeframe)).substr(0, 80));
    return timeframe;
}

inline std::int64_t symbol_bar_close(std::int64_t timestamp, const std::string& timeframe) {
    const auto count = std::stoll(timeframe);
    const auto unit = timeframe.back();
    if (unit != 'M') {
        const std::int64_t milliseconds = unit == 'S' ? 1000 : unit == 'D' ? 86400000 :
                                          unit == 'W' ? 604800000 : 60000;
        return timestamp + count * milliseconds;
    }
    const auto seconds = timestamp / 1000 - (timestamp % 1000 < 0 ? 1 : 0);
    const auto remainder = timestamp - seconds * 1000;
    const auto raw_time = static_cast<std::time_t>(seconds);
    std::tm calendar{};
    if (!::gmtime_r(&raw_time, &calendar) || calendar.tm_year < -1899 || calendar.tm_year > 8099)
        return symbol_stamp_max + 1;
    const auto month = calendar.tm_mon + count;
    const auto year = calendar.tm_year + 1900 + month / 12;
    if (year < 1 || year > 9999)
        return symbol_stamp_max + 1;
    calendar.tm_year = static_cast<int>(year - 1900);
    calendar.tm_mon = static_cast<int>(month % 12);
    constexpr std::array<int, 12> days{31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    const auto leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    calendar.tm_mday = std::min(calendar.tm_mday,
        days[calendar.tm_mon] + (calendar.tm_mon == 1 && leap ? 1 : 0));
    return static_cast<std::int64_t>(::timegm(&calendar)) * 1000 + remainder;
}

inline bool symbol_csv_row(std::istream& input, std::vector<std::string>& cells,
                           std::size_t& line, const std::string& where) {
    cells.clear();
    std::string cell;
    bool quoted = false;
    bool started = false;
    while (true) {
        if (cell.size() > 4 * symbol_csv_field_max)
            symbol_feed_error(where + ": CSV field exceeds 131072 characters");
        const auto next = input.get();
        if (next == EOF) {
            if (!started)
                return false;
            cells.push_back(std::move(cell));
            return true;
        }
        const char character = static_cast<char>(next);
        if (character == '\r' || character == '\n') {
            const bool paired_newline = character == '\r' && input.peek() == '\n';
            if (paired_newline)
                input.get();
            ++line;
            if (quoted) {
                cell += character;
                if (paired_newline)
                    cell += '\n';
                continue;
            }
            if (!started)
                return true;
            cells.push_back(std::move(cell));
            return true;
        }
        started = true;
        if (quoted) {
            if (character == '"') {
                if (input.peek() == '"') {
                    input.get();
                    cell += '"';
                } else {
                    quoted = false;
                }
            } else {
                cell += character;
            }
        } else if (character == ',') {
            cells.push_back(std::move(cell));
            cell.clear();
        } else if (character == '"' && cell.empty()) {
            quoted = true;
        } else {
            cell += character;
        }
    }
}

inline std::string symbol_numeric_unicode(std::string value) {
    if (std::none_of(value.begin(), value.end(), [](unsigned char character) {
            return character >= 128;
        }))
        return value;
    symbol_utf8_length(value);
    constexpr std::array<unsigned, 68> decimal_starts{
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6,
        0xc66, 0xce6, 0xd66, 0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0,
        0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620,
        0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0, 0x10d30, 0x11066,
        0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0, 0x11730,
        0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60, 0x16ac0, 0x16b50,
        0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e950,
        0x1fbf0};
    std::string normalized;
    for (std::size_t offset = 0; offset < value.size();) {
        const auto first = static_cast<unsigned char>(value[offset++]);
        if (first < 128) {
            normalized += static_cast<char>(first);
            continue;
        }
        const unsigned tails = first >= 0xf0 ? 3 : first >= 0xe0 ? 2 : 1;
        unsigned point = first & ((1u << (6 - tails)) - 1);
        for (unsigned index = 0; index < tails; ++index)
            point = (point << 6) | (static_cast<unsigned char>(value[offset++]) & 63);
        if (point == 0x85 || point == 0xa0 || point == 0x1680 ||
            (point >= 0x2000 && point <= 0x200a) || point == 0x2028 || point == 0x2029 ||
            point == 0x202f || point == 0x205f || point == 0x3000) {
            normalized += ' ';
            continue;
        }
        const auto following = std::upper_bound(decimal_starts.begin(),
                                                decimal_starts.end(), point);
        if (following == decimal_starts.begin() || point - *(following - 1) >= 10)
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "symbol_feeds"}}, "not a number");
        normalized += static_cast<char>('0' + point - *(following - 1));
    }
    return normalized;
}

inline std::string symbol_numeric_cell(std::string value, bool optional = false) {
    value = symbol_numeric_unicode(std::move(value));
    const auto* whitespace = optional ? " \t\r\n\f\v\x1c\x1d\x1e\x1f" : " \t\r\n\f\v";
    const auto first = value.find_first_not_of(whitespace);
    if (first == std::string::npos)
        return {};
    value = value.substr(first, value.find_last_not_of(whitespace) - first + 1);
    for (std::size_t index = 0; index < value.size(); ++index) {
        if (value[index] == '_' && (index == 0 || index + 1 == value.size() ||
            value[index - 1] < '0' || value[index - 1] > '9' ||
            value[index + 1] < '0' || value[index + 1] > '9'))
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "symbol_feeds"}}, "not a number");
    }
    value.erase(std::remove(value.begin(), value.end(), '_'), value.end());
    return value;
}

inline double symbol_double(const std::string& cell) {
    const auto text = symbol_numeric_cell(cell);
    if (text.empty() || text.find_first_of("xXpP") != std::string::npos ||
        text.find('\0') != std::string::npos || text.find('(') != std::string::npos)
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "symbol_feeds"}}, "not a number");
    char* end = nullptr;
    const auto number = std::strtod(text.c_str(), &end);
    if (end != text.c_str() + text.size())
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "symbol_feeds"}}, "not a number");
    return number;
}

inline std::int64_t symbol_integer(const std::string& cell) {
    const auto text = symbol_numeric_cell(cell);
    if (text.empty() || text.find_first_not_of("+-0123456789") != std::string::npos)
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "symbol_feeds"}}, "not a number");
    std::size_t consumed = 0;
    const auto number = std::stoll(text, &consumed);
    if (consumed != text.size())
        throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                   {{"reason", "symbol_feeds"}}, "not a number");
    return number;
}

inline void symbol_hash_integer(std::string& bytes, std::uint64_t value) {
    for (unsigned index = 0; index < 8; ++index) {
        bytes += static_cast<char>(value & 255);
        value >>= 8;
    }
}

inline SymbolFeed load_symbol_feed(const std::filesystem::path& path,
                                  const std::string& symbol, const std::string& timeframe) {
    const auto where = "feed " + symbol + "@" + timeframe + " (" + path.string() + ")";
    std::ifstream input(path, std::ios::binary);
    if (!input)
        symbol_feed_error(where + ": cannot open CSV");
    if (input.peek() == 0xef) {
        std::array<char, 3> bom{};
        input.read(bom.data(), bom.size());
        if (std::string(bom.data(), bom.size()) != "\xef\xbb\xbf") {
            input.clear();
            input.seekg(0);
        }
    }
    std::vector<std::string> cells;
    std::size_t line = 1;
    symbol_csv_row(input, cells, line, where);
    for (const auto& cell : cells)
        symbol_csv_cell(cell, where);
    std::map<std::string, std::size_t> columns;
    for (std::size_t index = 0; index < cells.size(); ++index)
        columns[cells[index]] = index;
    std::string missing;
    for (const auto* name : {"timestamp", "open", "high", "low", "close"})
        if (!columns.count(name))
            missing += (missing.empty() ? "" : ", ") + std::string(name);
    if (!missing.empty())
        symbol_feed_error(where + ": no column " + missing);
    SymbolFeed feed;
    feed.timeframe = timeframe;
    const auto field_cell = [&](const std::string& name) -> std::string {
        const auto found = columns.find(name);
        return found == columns.end() || found->second >= cells.size() ? "" : cells[found->second];
    };
    const char prefix[] = "pineforge:symbol-feed:barc-close-le:v1\0";
    std::string hashed(prefix, sizeof(prefix) - 1);
    std::string previous_at;
    while (symbol_csv_row(input, cells, line, where)) {
        if (cells.empty())
            continue;
        const auto at = where + " line " + std::to_string(input.eof() ? line : line - 1);
        for (const auto& cell : cells)
            symbol_csv_cell(cell, at);
        pf_bar_t bar{};
        std::int64_t close = 0;
        try {
            bar.timestamp = symbol_integer(field_cell("timestamp"));
            bar.open = symbol_double(field_cell("open"));
            bar.high = symbol_double(field_cell("high"));
            bar.low = symbol_double(field_cell("low"));
            bar.close = symbol_double(field_cell("close"));
            const auto volume = symbol_numeric_cell(field_cell("volume"), true);
            bar.volume = volume.empty() ? std::numeric_limits<double>::quiet_NaN() :
                                          symbol_double(volume);
            const auto close_cell = symbol_numeric_cell(field_cell("time_close"), true);
            close = close_cell.empty() ?
                (bar.timestamp < -symbol_stamp_max || bar.timestamp > symbol_stamp_max ?
                    symbol_stamp_max + 1 : symbol_bar_close(bar.timestamp, timeframe)) :
                symbol_integer(close_cell);
        } catch (const std::exception&) {
            symbol_feed_error(at + ": not a number");
        }
        if (!std::isfinite(bar.open) || !std::isfinite(bar.high) || !std::isfinite(bar.low) ||
            !std::isfinite(bar.close) || bar.volume < 0 || std::isinf(bar.volume))
            symbol_feed_error(at + ": prices must be finite and volume nonnegative or empty");
        if (bar.timestamp < -symbol_stamp_max || bar.timestamp > symbol_stamp_max ||
            close < -symbol_stamp_max || close > symbol_stamp_max)
            symbol_feed_error(at + ": a time must be unix milliseconds within +-" +
                              std::to_string(symbol_stamp_max));
        if (!feed.bars.empty() && feed.bars.back().timestamp >= bar.timestamp)
            symbol_feed_error(at + ": timestamps must increase");
        const auto bad_close = [&](const std::string& location, std::int64_t open_ms,
                                   std::int64_t close_ms) {
            symbol_feed_error(location + ": its close " + std::to_string(close_ms) +
                " is not after its open " + std::to_string(open_ms) +
                " and at or before the next bar's open (is the timeframe right?)");
        };
        if (!feed.close_ms.empty() && feed.close_ms.back() > bar.timestamp)
            bad_close(previous_at, feed.bars.back().timestamp, feed.close_ms.back());
        if (close <= bar.timestamp)
            bad_close(at, bar.timestamp, close);
        for (const auto value : {bar.open, bar.high, bar.low, bar.close, bar.volume}) {
            std::uint64_t bits = 0;
            std::memcpy(&bits, &value, sizeof(bits));
            symbol_hash_integer(hashed, bits);
        }
        symbol_hash_integer(hashed, static_cast<std::uint64_t>(bar.timestamp));
        symbol_hash_integer(hashed, static_cast<std::uint64_t>(close));
        feed.bars.push_back(bar);
        feed.close_ms.push_back(close);
        previous_at = at;
        if (feed.bars.size() > static_cast<std::size_t>(INT32_MAX))
            symbol_feed_error(where + ": exceeds the C ABI bar-count limit");
    }
    if (input.bad())
        symbol_feed_error(where + ": cannot read CSV");
    feed.source_values_sha256 = sha256(hashed);
    return feed;
}

inline SymbolFeeds load_symbol_feeds(const Json& document, const std::filesystem::path& directory) {
    const auto* symbols = document.kind == Json::Kind::Object ? document.find("symbols") : nullptr;
    if (!symbols || symbols->kind != Json::Kind::Object)
        symbol_feed_error("the index must be {\"symbols\": {...}}");
    if (symbols->members.size() > 256)
        symbol_feed_error("more than 256 symbols");
    SymbolFeeds result;
    std::size_t total = 0;
    for (const auto& item : symbols->members) {
        SymbolData symbol;
        symbol.symbol = symbol_text(Json::string(item.first), "a symbol");
        const auto& entry = item.second;
        if (entry.kind != Json::Kind::Object ||
            std::any_of(entry.members.begin(), entry.members.end(), [](const auto& member) {
                return member.first != "syminfo" && member.first != "feeds";
            }))
            symbol_feed_error(symbol.symbol +
                              ": an entry is {\"feeds\": {...}, \"syminfo\": {...}}");
        const auto* info = entry.find("syminfo");
        if (info && info->kind != Json::Kind::Null) {
            if (info->kind == Json::Kind::Object && info->find("syminfo"))
                info = info->find("syminfo");
            if (info->kind != Json::Kind::Object)
                symbol_feed_error(symbol.symbol + ": syminfo must be an object");
            for (const auto* name : {"tickerid", "type", "timezone", "session", "currency",
                                     "mintick"}) {
                const auto* value = info->find(name);
                if (!value || value->kind == Json::Kind::Null ||
                    (value->kind == Json::Kind::String && value->value.empty()))
                    continue;
                if (std::string(name) == "mintick") {
                    double number = 0;
                    try {
                        number = symbol_mintick(*value);
                    } catch (const std::exception&) {
                    }
                    if (value->kind != Json::Kind::Number || !(number > 0) ||
                        !std::isfinite(number))
                        symbol_feed_error(symbol.symbol +
                            ": syminfo.mintick must be a positive finite number, got " +
                            dump_json(*value).substr(0, 80));
                    symbol.facts.push_back({"mintick", number});
                } else {
                    symbol.facts.push_back({std::string(name) == "tickerid" ? "canonical" : name,
                        symbol_text(*value, symbol.symbol + ": syminfo." + name)});
                }
            }
        }
        const auto* feeds = entry.find("feeds");
        if (feeds) {
            if (feeds->kind != Json::Kind::Object)
                symbol_feed_error(symbol.symbol + ": feeds must be an object");
            total += feeds->members.size();
            if (total > 256)
                symbol_feed_error("more than 256 feeds (" + symbol.symbol + ")");
            std::map<std::string, std::filesystem::path> named;
            for (const auto& feed : feeds->members) {
                const auto timeframe = symbol_timeframe(feed.first,
                                                        symbol.symbol + "@" + feed.first);
                if (named.count(timeframe))
                    symbol_feed_error(symbol.symbol + ": two feeds at timeframe " + timeframe);
                if (feed.second.kind != Json::Kind::String || feed.second.value.empty() ||
                    feed.second.value.find('\0') != std::string::npos)
                    symbol_feed_error(symbol.symbol + "@" + timeframe +
                                      ": the feed must name a CSV file");
                named.emplace(timeframe, directory / feed.second.value);
            }
            for (const auto& feed : named) {
                symbol.feeds.push_back(load_symbol_feed(feed.second, symbol.symbol, feed.first));
            }
        }
        result.push_back(std::move(symbol));
    }
    return result;
}

inline SymbolFeeds load_symbol_feeds(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input)
        symbol_feed_error(path.string() + ": " + std::strerror(errno));
    std::ostringstream text;
    text << input.rdbuf();
    Json document;
    try {
        document = parse_json(text.str(), std::numeric_limits<std::size_t>::max());
    } catch (const std::exception& error) {
        symbol_feed_error(path.string() + " is not JSON: " + error.what());
    }
    return load_symbol_feeds(document, path.parent_path());
}

inline Json symbol_feeds_record(const SymbolFeeds& symbols) {
    auto record = object_json();
    record.members["canonicalization"] = Json::string(symbol_feed_canonicalization);
    auto entries = object_json();
    for (const auto& symbol : symbols) {
        auto entry = object_json();
        auto facts = object_json();
        for (const auto& fact : symbol.facts) {
            facts.members[fact.field] = std::holds_alternative<double>(fact.value) ?
                scalar_json(std::get<double>(fact.value)) :
                Json::string(std::get<std::string>(fact.value));
        }
        entry.members["facts"] = std::move(facts);
        auto feeds = object_json();
        for (const auto& feed : symbol.feeds) {
            auto value = object_json();
            value.members["bars"] = Json::number(std::to_string(feed.bars.size()));
            value.members["source_values_sha256"] = Json::string(feed.source_values_sha256);
            if (!feed.bars.empty()) {
                value.members["first_ts"] = Json::number(
                    std::to_string(feed.bars.front().timestamp));
                value.members["last_ts"] = Json::number(std::to_string(feed.bars.back().timestamp));
            }
            feeds.members[feed.timeframe] = std::move(value);
        }
        entry.members["feeds"] = std::move(feeds);
        entries.members[symbol.symbol] = std::move(entry);
    }
    record.members["symbols"] = std::move(entries);
    return record;
}

inline SymbolFeeds symbol_feeds_from_spec(const Json& document,
                                         const std::filesystem::path& spec) {
    if (const auto* source = document.find("symbol_feeds"); source &&
        source->kind != Json::Kind::Null) {
        return source->kind == Json::Kind::String ?
            load_symbol_feeds(spec.parent_path() / source->value) :
            load_symbol_feeds(*source, spec.parent_path());
    }
    return {};
}

inline SymbolFeeds load_symbol_feeds_spec(const std::filesystem::path& spec) {
    return symbol_feeds_from_spec(parse_json(read_document(spec), 256 * 1024 * 1024), spec);
}

inline Json space_with_symbol_feeds(const std::filesystem::path& spec,
                                   const std::filesystem::path& override_index = {}) {
    const auto document = parse_json(read_document(spec), 256 * 1024 * 1024);
    auto recorded = space_from_spec(document);
    const auto symbols = override_index.empty() ? symbol_feeds_from_spec(document, spec) :
                                                 load_symbol_feeds(override_index);
    if (!symbols.empty())
        recorded.members["symbol_feeds"] = symbol_feeds_record(symbols);
    return recorded;
}

}
