#pragma once

#include <pineforge/hpo/error.hpp>

#include <algorithm>
#include <array>
#include <cstdint>
#include <istream>
#include <ostream>
#include <stdexcept>
#include <string>

namespace pineforge::hpo::detail {

class Mt19937_64 {
public:
    explicit Mt19937_64(std::uint64_t value = 5489) { seed(value); }

    void seed(std::uint64_t value) {
        words_[0] = value;
        for (std::uint64_t index = 1; index < words_.size(); ++index)
            words_[index] = 6364136223846793005ULL *
                (words_[index - 1] ^ (words_[index - 1] >> 62)) + index;
        position_ = words_.size();
    }

    std::uint64_t operator()() {
        if (position_ == words_.size()) {
            for (std::size_t index = 0; index < words_.size(); ++index) {
                const auto mixed = (words_[index] & 0xffffffff80000000ULL) |
                    (words_[(index + 1) % words_.size()] & 0x7fffffffULL);
                words_[index] = words_[(index + 156) % words_.size()] ^ (mixed >> 1) ^
                    ((mixed & 1) ? 0xb5026f5aa96619e9ULL : 0);
            }
            position_ = 0;
        }
        auto value = words_[position_++];
        value ^= (value >> 29) & 0x5555555555555555ULL;
        value ^= (value << 17) & 0x71d67fffeda60000ULL;
        value ^= (value << 37) & 0xfff7eee000000000ULL;
        return value ^ (value >> 43);
    }

    void discard(std::uint64_t count) {
        while (count--)
            (*this)();
    }

    void write(std::ostream& output) const {
        output << "MT64 312 ";
        for (const auto word : words_)
            output << word << ' ';
        output << position_ << '\n';
    }

    void read(std::istream& input) {
        std::string marker;
        std::uint64_t count, position;
        std::array<std::uint64_t, 312> words{};
        if (!(input >> marker >> count) || marker != "MT64" || count != words.size())
            throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                       {{"reason", "sampler"}},
                                                       "invalid canonical MT state header");
        for (auto& word : words)
            if (!(input >> word))
                throw TypedHpoError<std::invalid_argument>("hpo_study_spec_invalid",
                                                           {{"reason", "sampler"}},
                                                           "invalid canonical MT state word");
        if (!(input >> position) || position > words.size() ||
            ((words[0] & 0xffffffff80000000ULL) == 0 &&
             std::all_of(words.begin() + 1, words.end(), [](auto word) { return word == 0; })))
            throw TypedHpoError<std::invalid_argument>(
                "hpo_study_spec_invalid", {{"reason", "sampler"}},
                "invalid canonical MT state position/zero state");
        words_ = words;
        position_ = static_cast<std::uint32_t>(position);
    }

private:
    std::array<std::uint64_t, 312> words_{};
    std::uint32_t position_ = 312;
};

}
