#pragma once

#include <cmath>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace pineforge::hpo::detail {

struct Json {
    enum class Kind { Null, Bool, Number, String, Array, Object };
    Kind kind = Kind::Null;
    std::string value;
    std::vector<Json> items;
    std::map<std::string, Json> members;

    static Json string(std::string value) {
        Json json;
        json.kind = Kind::String;
        json.value = std::move(value);
        return json;
    }
    static Json number(std::string value) {
        Json json;
        json.kind = Kind::Number;
        json.value = std::move(value);
        return json;
    }
    static Json boolean(bool value) {
        Json json;
        json.kind = Kind::Bool;
        json.value = value ? "true" : "false";
        return json;
    }
    const Json* find(const std::string& key) const {
        if (kind != Kind::Object)
            throw std::runtime_error("expected object");
        auto found = members.find(key);
        return found == members.end() ? nullptr : &found->second;
    }
    std::string text() const {
        if (kind != Kind::String)
            throw std::runtime_error("expected string");
        return value;
    }
    double real() const {
        if (kind != Kind::Number)
            throw std::runtime_error("expected number");
        std::size_t consumed = 0;
        double result = std::stod(value, &consumed);
        if (consumed != value.size() || !std::isfinite(result))
            throw std::runtime_error("nonfinite number");
        return result;
    }
};

class JsonParser {
    std::string_view input_;
    std::size_t position_ = 0;
    [[noreturn]] void fail() const {
        throw std::runtime_error("invalid JSON at byte " + std::to_string(position_));
    }
    void skip_whitespace() {
        while (position_ < input_.size() &&
               (input_[position_] == ' ' || input_[position_] == '\t' || input_[position_] == '\r' || input_[position_] == '\n'))
            ++position_;
    }
    bool take(char character) {
        if (position_ < input_.size() && input_[position_] == character) {
            ++position_;
            return true;
        }
        return false;
    }
    unsigned parse_hex4() {
        unsigned value = 0;
        for (int index = 0; index < 4; ++index) {
            if (position_ == input_.size())
                fail();
            char character = input_[position_++];
            value <<= 4;
            if (character >= '0' && character <= '9')
                value += character - '0';
            else if (character >= 'a' && character <= 'f')
                value += character - 'a' + 10;
            else if (character >= 'A' && character <= 'F')
                value += character - 'A' + 10;
            else
                fail();
        }
        return value;
    }
    static void append_utf8(std::string& out, unsigned character) {
        if (character < 0x80)
            out += static_cast<char>(character);
        else if (character < 0x800) {
            out += static_cast<char>(0xc0 | (character >> 6));
            out += static_cast<char>(0x80 | (character & 63));
        } else if (character < 0x10000) {
            out += static_cast<char>(0xe0 | (character >> 12));
            out += static_cast<char>(0x80 | ((character >> 6) & 63));
            out += static_cast<char>(0x80 | (character & 63));
        } else {
            out += static_cast<char>(0xf0 | (character >> 18));
            out += static_cast<char>(0x80 | ((character >> 12) & 63));
            out += static_cast<char>(0x80 | ((character >> 6) & 63));
            out += static_cast<char>(0x80 | (character & 63));
        }
    }
    std::string parse_string() {
        if (!take('"'))
            fail();
        std::string out;
        while (position_ < input_.size()) {
            unsigned char character = input_[position_++];
            if (character == '"')
                return out;
            if (character < 32)
                fail();
            if (character != '\\') {
                if (character < 128) {
                    out += static_cast<char>(character);
                    continue;
                }
                unsigned continuation_count = character >= 0xf0 ? 3 : character >= 0xe0 ? 2 : character >= 0xc2 ? 1 : 0;
                if (!continuation_count || character > 0xf4)
                    fail();
                unsigned point = character & ((1u << (6 - continuation_count)) - 1);
                for (unsigned index = 0; index < continuation_count; ++index) {
                    if (position_ == input_.size())
                        fail();
                    unsigned char tail = input_[position_++];
                    if ((tail & 0xc0) != 0x80)
                        fail();
                    point = (point << 6) | (tail & 63);
                }
                if (point < (continuation_count == 1   ? 0x80u
                             : continuation_count == 2 ? 0x800u
                                      : 0x10000u) ||
                    point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff))
                    fail();
                append_utf8(out, point);
                continue;
            }
            if (position_ == input_.size())
                fail();
            char escape = input_[position_++];
            switch (escape) {
            case '"':
            case '\\':
            case '/':
                out += escape;
                break;
            case 'b':
                out += '\b';
                break;
            case 'f':
                out += '\f';
                break;
            case 'n':
                out += '\n';
                break;
            case 'r':
                out += '\r';
                break;
            case 't':
                out += '\t';
                break;
            case 'u': {
                unsigned point = parse_hex4();
                if (point >= 0xd800 && point <= 0xdbff) {
                    if (!take('\\') || !take('u'))
                        fail();
                    unsigned low = parse_hex4();
                    if (low < 0xdc00 || low > 0xdfff)
                        fail();
                    point = 0x10000 + ((point - 0xd800) << 10) + (low - 0xdc00);
                } else if (point >= 0xdc00 && point <= 0xdfff)
                    fail();
                append_utf8(out, point);
                break;
            }
            default:
                fail();
            }
        }
        fail();
    }
    Json parse(unsigned depth) {
        if (depth >= 32)
            fail();
        skip_whitespace();
        if (position_ == input_.size())
            fail();
        if (input_[position_] == '"')
            return Json::string(parse_string());
        if (take('{')) {
            Json json;
            json.kind = Json::Kind::Object;
            skip_whitespace();
            if (take('}'))
                return json;
            do {
                skip_whitespace();
                auto key = parse_string();
                skip_whitespace();
                if (!take(':'))
                    fail();
                auto value = parse(depth + 1);
                if (!json.members.emplace(std::move(key), std::move(value)).second)
                    fail();
                skip_whitespace();
                if (take('}'))
                    return json;
            } while (take(','));
            fail();
        }
        if (take('[')) {
            Json json;
            json.kind = Json::Kind::Array;
            skip_whitespace();
            if (take(']'))
                return json;
            do {
                json.items.push_back(parse(depth + 1));
                skip_whitespace();
                if (take(']'))
                    return json;
            } while (take(','));
            fail();
        }
        for (auto lit : {"null", "true", "false"}) {
            std::string_view value = lit;
            if (input_.substr(position_, value.size()) == value) {
                position_ += value.size();
                return value == "null" ? Json{} : Json::boolean(value == "true");
            }
        }
        std::size_t begin = position_;
        take('-');
        if (take('0')) {
        } else {
            if (position_ == input_.size() || input_[position_] < '1' || input_[position_] > '9')
                fail();
            while (position_ < input_.size() && input_[position_] >= '0' && input_[position_] <= '9')
                ++position_;
        }
        if (take('.')) {
            auto old = position_;
            while (position_ < input_.size() && input_[position_] >= '0' && input_[position_] <= '9')
                ++position_;
            if (position_ == old)
                fail();
        }
        if (take('e') || take('E')) {
            if (!take('+'))
                take('-');
            auto old = position_;
            while (position_ < input_.size() && input_[position_] >= '0' && input_[position_] <= '9')
                ++position_;
            if (position_ == old)
                fail();
        }
        return Json::number(std::string(input_.substr(begin, position_ - begin)));
    }

public:
    explicit JsonParser(std::string_view input) : input_(input) {
        if (input.size() > 1024 * 1024)
            throw std::runtime_error("JSON frame exceeds 1 MiB");
    }
    Json run() {
        auto json = parse(0);
        skip_whitespace();
        if (position_ != input_.size())
            fail();
        return json;
    }
};
inline Json parse_json(std::string_view input) { return JsonParser(input).run(); }
}
