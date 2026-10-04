#pragma once

#include "continuation.hpp"

#include <array>
#include <cmath>
#include <iomanip>
#include <numeric>
#include <sstream>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace pineforge::hpo::detail {

inline constexpr std::array<unsigned char, 8> warm_state_magic{
    'P', 'F', 'H', 'S', 'T', 'A', 'T', 'E'};

inline constexpr std::array<unsigned char, 8> warm_magic{'P', 'F', 'H', 'W', 'A', 'R', 'M', 0};
inline constexpr std::uint64_t warm_null = 0x7ff8000000000000ULL;
inline constexpr std::array<const char*, 9> warm_states{
    "ok", "constraint_violation", "engine_error", "objective_error", "constraint_error",
    "trial_error", "trial_timeout", "pruned", "partial"};

inline std::vector<std::size_t> warm_constraint_order(const Json& recorded) {
    const auto& constraints = field(field(recorded, "objective"), "constraints").items;
    std::vector<std::size_t> order(constraints.size());
    std::iota(order.begin(), order.end(), 0U);
    std::stable_sort(order.begin(), order.end(), [&](auto left, auto right) {
        return constraints[left].text() < constraints[right].text();
    });
    return order;
}

template <class Integer>
inline Integer warm_integer(const unsigned char* bytes) {
    Integer value = 0;
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
    std::memcpy(&value, bytes, sizeof(value));
#else
    for (std::size_t index = 0; index < sizeof(value); ++index)
        value |= static_cast<Integer>(bytes[index]) << (index * 8);
#endif
    return value;
}

inline double warm_real(const unsigned char* bytes) {
    const auto bits = warm_integer<std::uint64_t>(bytes);
    double value;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

inline int warm_compare_real(double left, double right) {
    if (left != right)
        return left < right ? -1 : 1;
    if (left == 0.0 && std::signbit(left) != std::signbit(right))
        return std::signbit(left) ? -1 : 1;
    return 0;
}

inline std::uint64_t warm_add(std::uint64_t left, std::uint64_t right) {
    if (right > std::numeric_limits<std::uint64_t>::max() - left)
        throw std::runtime_error("binary block length overflow");
    return left + right;
}

inline std::uint64_t warm_multiply(std::uint64_t left, std::uint64_t right) {
    if (left && right > std::numeric_limits<std::uint64_t>::max() / left)
        throw std::runtime_error("binary block length overflow");
    return left * right;
}

class WarmParameterCodec {
public:
    explicit WarmParameterCodec(Dimension dimension)
        : dimension_(std::move(dimension)), single_({dimension_}),
          name_(dimension_name(dimension_)) {
        kind = std::holds_alternative<IntegerDimension>(dimension_) ? 1 :
            std::holds_alternative<RealDimension>(dimension_) ? 2 :
            std::holds_alternative<BooleanDimension>(dimension_) ? 3 : 4;
        const auto* real = std::get_if<RealDimension>(&dimension_);
        encoding = real && !real->step() ? 2 : 1;
        if (encoding == 1) {
            count_ = single_.finite_cardinality().value();
            if (count_ > std::uint64_t{1} + std::numeric_limits<std::int32_t>::max())
                throw std::runtime_error("binary parameter grid index exceeds int32: " + name_);
        }
    }

    ParameterValue decode(std::uint32_t index) const {
        return std::visit([&](const auto& dimension) -> ParameterValue {
            using Type = std::decay_t<decltype(dimension)>;
            if constexpr (std::is_same_v<Type, IntegerDimension>) {
                const auto offset = static_cast<std::uint64_t>(index) * dimension.step();
                if (dimension.low() >= 0)
                    return dimension.low() + static_cast<std::int64_t>(offset);
                const auto zero = 0U - static_cast<std::uint64_t>(dimension.low());
                return offset < zero ? dimension.low() + static_cast<std::int64_t>(offset) :
                    static_cast<std::int64_t>(offset - zero);
            } else if constexpr (std::is_same_v<Type, RealDimension>) {
                if (dimension.low() == dimension.high())
                    return dimension.low();
                auto value = std::fma(static_cast<double>(index), *dimension.step(),
                                      dimension.low());
                if (index + std::uint64_t{1} == count_ && value > dimension.high())
                    value = dimension.high();
                return value;
            } else if constexpr (std::is_same_v<Type, BooleanDimension>) {
                return index != 0;
            } else {
                return dimension.choices()[index];
            }
        }, dimension_);
    }

    std::uint32_t encode(const ParameterValue& value) const {
        Candidate candidate;
        candidate.values.emplace(name_, value);
        const auto index = single_.candidate_ordinal(candidate);
        const auto decoded = decode(static_cast<std::uint32_t>(index));
        if (decoded != value || (std::holds_alternative<double>(value) &&
            warm_compare_real(std::get<double>(decoded), std::get<double>(value)) != 0))
            throw std::runtime_error("noncanonical binary grid parameter: " + name_);
        return static_cast<std::uint32_t>(index);
    }

    bool valid_index(std::uint32_t index) const noexcept { return index < count_; }
    bool valid_real(double value) const noexcept {
        const auto& dimension = std::get<RealDimension>(dimension_);
        return std::isfinite(value) && value >= dimension.low() && value <= dimension.high();
    }
    const std::string& name() const noexcept { return name_; }
    std::uint64_t count() const noexcept { return count_; }

    std::uint8_t kind = 0;
    std::uint8_t encoding = 0;

private:
    Dimension dimension_;
    SearchSpace single_;
    std::string name_;
    std::uint64_t count_ = 0;
};

class WarmMapping final {
public:
    explicit WarmMapping(const std::filesystem::path& path) {
        const auto descriptor = ::open(path.c_str(), O_RDONLY);
        if (descriptor < 0)
            throw std::runtime_error("cannot open binary warm input");
        struct stat info {};
        if (::fstat(descriptor, &info) != 0 || info.st_size <= 0 ||
            static_cast<std::uint64_t>(info.st_size) >
                std::numeric_limits<std::size_t>::max()) {
            ::close(descriptor);
            throw std::runtime_error("invalid binary warm file length");
        }
        size = static_cast<std::size_t>(info.st_size);
#if defined(POSIX_FADV_SEQUENTIAL)
        ::posix_fadvise(descriptor, 0, 0, POSIX_FADV_SEQUENTIAL);
#endif
        auto* mapping = ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, descriptor, 0);
        ::close(descriptor);
        if (mapping == MAP_FAILED)
            throw std::runtime_error("cannot map binary warm input");
        ::madvise(mapping, size, MADV_SEQUENTIAL);
        bytes = static_cast<const unsigned char*>(mapping);
    }
    ~WarmMapping() { ::munmap(const_cast<unsigned char*>(bytes), size); }
    WarmMapping(const WarmMapping&) = delete;
    WarmMapping& operator=(const WarmMapping&) = delete;

    const unsigned char* bytes = nullptr;
    std::size_t size = 0;
};

class BinaryWarmSource final : public WarmStartSource {
public:
    BinaryWarmSource(const std::filesystem::path& path, const SearchSpace& space,
                     const Json& recorded) : mapping_(path), space_(space) {
        std::map<std::string, Dimension> sorted;
        for (const auto& dimension : space.dimensions())
            sorted.emplace(std::string(dimension_name(dimension)), dimension);
        for (const auto& entry : sorted)
            codecs_.emplace_back(entry.second);
        for (const auto& dimension : space.dimensions()) {
            const auto found = sorted.find(std::string(dimension_name(dimension)));
            columns_.push_back(static_cast<std::size_t>(std::distance(sorted.begin(), found)));
        }
        const auto hash = space_hash(recorded);
        const auto constraints = field(field(recorded, "objective"), "constraints").items.size();
        const auto constraint_order = warm_constraint_order(recorded);
        constraint_columns_.resize(constraints);
        for (std::size_t column = 0; column < constraints; ++column)
            constraint_columns_[constraint_order[column]] = column;
        std::array<unsigned char, 32> digest{};
        for (std::size_t index = 0; index < digest.size(); ++index)
            digest[index] = static_cast<unsigned char>(std::stoul(hash.substr(index * 2, 2),
                                                                 nullptr, 16));
        std::uint64_t offset = 0;
        std::uint64_t checkpoint_rows = 0;
        std::map<std::uint64_t, std::string> checkpoints;
        while (offset < mapping_.size) {
            if (mapping_.size - offset >= 8 &&
                std::equal(warm_state_magic.begin(), warm_state_magic.end(),
                           mapping_.bytes + offset)) {
                if (mapping_.size - offset < 16)
                    throw std::runtime_error("truncated sampler-state block header");
                const auto bytes = warm_integer<std::uint64_t>(mapping_.bytes + offset + 8);
                if (!bytes || bytes > 16 * 1024 * 1024 || bytes > mapping_.size - offset - 16)
                    throw std::runtime_error("invalid sampler-state block length");
                const std::string state(
                    reinterpret_cast<const char*>(mapping_.bytes + offset + 16),
                    static_cast<std::size_t>(bytes));
                if (state.size() < 73 || state.substr(0, 8) != "PFHTPE2\n" ||
                    state[72] != '\n' || sha256(std::string_view(state).substr(73)) !=
                        state.substr(8, 64))
                    throw std::runtime_error("invalid sampler-state checksum/version");
                std::istringstream payload(state.substr(73));
                payload.imbue(std::locale::classic());
                std::string signature, numeric_build;
                std::uint64_t rows;
                if (!(payload >> std::quoted(signature) >> std::quoted(numeric_build) >> rows))
                    throw std::runtime_error("invalid sampler-state header");
                const auto checksum = state.substr(8, 64);
                const auto inserted = checkpoints.emplace(rows, checksum);
                if (!inserted.second && inserted.first->second != checksum)
                    throw std::runtime_error("conflicting sampler-state checkpoints");
                if (sampler_state.empty() || rows > checkpoint_rows) {
                    sampler_state = state;
                    checkpoint_rows = rows;
                }
                offset += 16 + bytes;
                continue;
            }
            if (mapping_.size - offset < 80)
                throw std::runtime_error("truncated binary block header");
            const auto* header = mapping_.bytes + offset;
            if (!std::equal(warm_magic.begin(), warm_magic.end(), header))
                throw std::runtime_error("invalid binary block magic");
            if (warm_integer<std::uint16_t>(header + 8) != 2)
                throw std::runtime_error("unsupported binary warm version");
            const auto header_bytes = warm_integer<std::uint32_t>(header + 12);
            const auto block_bytes = warm_integer<std::uint64_t>(header + 16);
            const auto count = warm_integer<std::uint64_t>(header + 24);
            if (warm_integer<std::uint16_t>(header + 10) != 0 ||
                warm_integer<std::uint32_t>(header + 44) != 0)
                throw std::runtime_error("unsupported binary block flags");
            if (!std::equal(digest.begin(), digest.end(), header + 48))
                throw std::runtime_error("search space or objective differs (space_hash)");
            if (warm_integer<std::uint32_t>(header + 32) != codecs_.size() ||
                warm_integer<std::uint32_t>(header + 36) != 1 ||
                warm_integer<std::uint32_t>(header + 40) != constraints)
                throw std::runtime_error("binary column counts do not match study");
            if (header_bytes != warm_add(80, warm_multiply(4, codecs_.size())) ||
                count == 0 || block_bytes < header_bytes || block_bytes > mapping_.size - offset)
                throw std::runtime_error("truncated or invalid binary block length");
            std::uint64_t row_bytes = warm_add(17, warm_multiply(8, constraints));
            for (std::size_t index = 0; index < codecs_.size(); ++index) {
                const auto* descriptor = header + 80 + index * 4;
                if (descriptor[0] != codecs_[index].kind ||
                    descriptor[1] != codecs_[index].encoding || descriptor[2] || descriptor[3])
                    throw std::runtime_error("binary parameter descriptor differs from study");
                row_bytes = warm_add(row_bytes, codecs_[index].encoding == 1 ? 4 : 8);
            }
            if (block_bytes != warm_add(header_bytes, warm_multiply(count, row_bytes)))
                throw std::runtime_error("invalid binary payload length");
            if (warm_add(rows_, count) > std::numeric_limits<std::uint32_t>::max())
                throw std::runtime_error("binary history exceeds uint32 row index capacity");
            Block block;
            block.begin = rows_;
            block.count = count;
            const auto* cursor = header + header_bytes;
            block.ids = cursor;
            cursor += count * 8;
            block.states = cursor;
            cursor += count;
            for (const auto& codec : codecs_) {
                block.parameters.push_back(cursor);
                cursor += count * (codec.encoding == 1 ? 4 : 8);
            }
            block.objectives = cursor;
            cursor += count * 8;
            for (std::size_t index = 0; index < constraints; ++index) {
                block.constraints.push_back(cursor);
                cursor += count * 8;
            }
            validate(block);
            blocks_.push_back(std::move(block));
            rows_ += count;
            offset += block_bytes;
        }
        if (!rows_)
            throw std::runtime_error("binary history contains no trials");
        bool ordered = true;
        for (std::uint64_t row = 1; row < rows_; ++row)
            ordered = ordered && raw_id(row - 1) < raw_id(row);
        if (!ordered) {
            order_.resize(static_cast<std::size_t>(rows_));
            std::iota(order_.begin(), order_.end(), 0U);
            std::sort(order_.begin(), order_.end(), [&](auto left, auto right) {
                return raw_id(left) < raw_id(right);
            });
            for (std::uint64_t row = 1; row < rows_; ++row)
                if (id(row - 1) == id(row))
                    throw std::runtime_error("duplicate trial_id");
        }
        tried_.resize(static_cast<std::size_t>(rows_));
        std::iota(tried_.begin(), tried_.end(), 0U);
        const auto less = [&](auto left, auto right) { return compare(left, right) < 0; };
        if (!std::is_sorted(tried_.begin(), tried_.end(), less))
            std::sort(tried_.begin(), tried_.end(), less);
        tried_.erase(std::unique(tried_.begin(), tried_.end(), [&](auto left, auto right) {
            return compare(left, right) == 0;
        }), tried_.end());
    }

    std::uint64_t size() const noexcept override { return rows_; }
    std::uint64_t id(std::uint64_t row) const override { return raw_id(raw_row(row)); }
    ParameterValue parameter(std::uint64_t row, std::size_t dimension) const override {
        const auto column = columns_.at(dimension);
        const auto [block, local] = locate(raw_row(row));
        const auto* bytes = block->parameters[column] + local *
            (codecs_[column].encoding == 1 ? 4 : 8);
        return codecs_[column].encoding == 1
            ? codecs_[column].decode(warm_integer<std::uint32_t>(bytes))
            : ParameterValue(warm_real(bytes));
    }
    std::optional<double> objective(std::uint64_t row) const override {
        const auto [block, local] = locate(raw_row(row));
        const auto value = warm_real(block->objectives + local * 8);
        return block->states[local] == 0 && std::isfinite(value)
            ? std::optional<double>(value) : std::nullopt;
    }
    std::uint64_t tried_count() const noexcept { return tried_.size(); }
    bool contains(const Candidate& candidate) const {
        std::vector<double> values;
        values.reserve(codecs_.size());
        for (const auto& codec : codecs_) {
            const auto& value = candidate.values.at(codec.name());
            values.push_back(codec.encoding == 1 ? codec.encode(value) : std::get<double>(value));
        }
        const auto compare_value = [&](std::uint32_t row) {
            for (std::size_t column = 0; column < values.size(); ++column) {
                const auto value = raw_parameter(row, column);
                const auto compared = warm_compare_real(value, values[column]);
                if (compared)
                    return compared;
            }
            return 0;
        };
        const auto found = std::lower_bound(tried_.begin(), tried_.end(), 0,
            [&](auto row, int) { return compare_value(row) < 0; });
        return found != tried_.end() && compare_value(*found) == 0;
    }
    std::uint64_t ordinal(const SearchSpace& space, std::uint64_t row) const {
        std::uint64_t result = 0;
        for (const auto column : columns_) {
            if (codecs_[column].encoding != 1)
                return space.candidate_ordinal(observation(space, row).candidate);
            result = result * codecs_[column].count() +
                static_cast<std::uint64_t>(raw_parameter(raw_row(row), column));
        }
        return result;
    }
    Json record(std::uint64_t row) const {
        const auto [block, local] = locate(raw_row(row));
        auto result = object_json();
        result.members["trial_id"] = Json::number(std::to_string(id(row)));
        result.members["status"] = Json::string(warm_states[block->states[local]]);
        result.members["feasible"] = Json::boolean(block->states[local] == 0);
        const auto score = warm_real(block->objectives + local * 8);
        result.members["objective"] = std::isfinite(score) ? scalar_json(score) : Json{};
        auto parameters = object_json();
        for (std::size_t index = 0; index < space_.dimensions().size(); ++index)
            parameters.members[std::string(dimension_name(space_.dimensions()[index]))] =
                scalar_json(parameter(row, index));
        result.members["parameters"] = std::move(parameters);
        auto constraints = array_json();
        for (const auto index : constraint_columns_) {
            const auto* column = block->constraints[index];
            const auto value = warm_real(column + local * 8);
            constraints.items.push_back(std::isfinite(value) ? scalar_json(value) : Json{});
        }
        result.members["constraint_values"] = std::move(constraints);
        return result;
    }
    std::string digest() const {
        return sha256(std::string_view(reinterpret_cast<const char*>(mapping_.bytes),
                                       mapping_.size));
    }

    std::uint64_t completed = 0;
    std::uint64_t feasible = 0;
    std::string sampler_state;

private:
    struct Block {
        std::uint64_t begin = 0;
        std::uint64_t count = 0;
        const unsigned char* ids = nullptr;
        const unsigned char* states = nullptr;
        std::vector<const unsigned char*> parameters;
        const unsigned char* objectives = nullptr;
        std::vector<const unsigned char*> constraints;
    };
    std::pair<const Block*, std::uint64_t> locate(std::uint64_t row) const {
        if (row >= rows_)
            throw std::out_of_range("binary warm row out of range");
        if (blocks_.size() == 1)
            return {&blocks_.front(), row};
        const auto found = std::upper_bound(blocks_.begin(), blocks_.end(), row,
            [](auto value, const auto& block) { return value < block.begin; });
        const auto* block = &*std::prev(found);
        return {block, row - block->begin};
    }
    std::uint64_t raw_row(std::uint64_t row) const {
        return order_.empty() ? row : order_.at(static_cast<std::size_t>(row));
    }
    std::uint64_t raw_id(std::uint64_t row) const {
        const auto [block, local] = locate(row);
        return warm_integer<std::uint64_t>(block->ids + local * 8);
    }
    double raw_parameter(std::uint64_t row, std::size_t column) const {
        const auto [block, local] = locate(row);
        return codecs_[column].encoding == 1
            ? static_cast<double>(warm_integer<std::uint32_t>(
                block->parameters[column] + local * 4))
            : warm_real(block->parameters[column] + local * 8);
    }
    int compare(std::uint32_t left, std::uint32_t right) const {
        for (std::size_t column = 0; column < codecs_.size(); ++column) {
            const auto left_value = raw_parameter(left, column);
            const auto right_value = raw_parameter(right, column);
            const auto compared = warm_compare_real(left_value, right_value);
            if (compared)
                return compared;
        }
        return 0;
    }
    static void validate_score(const unsigned char* bytes) {
        if (!std::isfinite(warm_real(bytes)) && warm_integer<std::uint64_t>(bytes) != warm_null)
            throw std::runtime_error("invalid binary objective or constraint value");
    }
    void validate(const Block& block) {
        for (std::uint64_t row = 0; row < block.count; ++row) {
            if (warm_integer<std::uint64_t>(block.ids + row * 8) ==
                std::numeric_limits<std::uint64_t>::max())
                throw std::runtime_error("trial_id leaves no continuation ID");
            const auto state = block.states[row];
            if (state >= warm_states.size())
                throw std::runtime_error("invalid binary trial state");
            completed += state <= 1;
            feasible += state == 0;
            validate_score(block.objectives + row * 8);
            for (const auto* constraint : block.constraints)
                validate_score(constraint + row * 8);
        }
        for (std::size_t column = 0; column < codecs_.size(); ++column) {
            for (std::uint64_t row = 0; row < block.count; ++row) {
                const auto* bytes = block.parameters[column] + row *
                    (codecs_[column].encoding == 1 ? 4 : 8);
                if (codecs_[column].encoding == 1
                    ? !codecs_[column].valid_index(warm_integer<std::uint32_t>(bytes))
                    : !codecs_[column].valid_real(warm_real(bytes)))
                    throw std::runtime_error("invalid binary parameter column: " +
                                             codecs_[column].name());
            }
        }
    }

    WarmMapping mapping_;
    SearchSpace space_;
    std::vector<WarmParameterCodec> codecs_;
    std::vector<std::size_t> columns_;
    std::vector<std::size_t> constraint_columns_;
    std::vector<Block> blocks_;
    std::vector<std::uint32_t> order_;
    std::vector<std::uint32_t> tried_;
    std::uint64_t rows_ = 0;
};

inline std::uint64_t WarmHistory::size() const {
    return binary ? binary->size() : observations.size();
}
inline std::uint64_t WarmHistory::tried_count() const {
    return binary ? binary->tried_count() : tried.size();
}
inline bool WarmHistory::contains(const Candidate& candidate) const {
    return binary ? binary->contains(candidate) : tried.count(candidate_key(candidate)) != 0;
}
inline std::uint64_t WarmHistory::ordinal(const SearchSpace& space, std::uint64_t row) const {
    return binary ? binary->ordinal(space, row) :
        space.candidate_ordinal(observations.at(static_cast<std::size_t>(row)).candidate);
}
inline Json WarmHistory::record(const SearchSpace&, std::uint64_t row) const {
    return binary ? binary->record(row) : records.at(static_cast<std::size_t>(row));
}
inline std::string WarmHistory::source_digest() const {
    return binary ? binary->digest() : source_sha256;
}

inline bool binary_warm_file(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    std::array<unsigned char, 8> prefix{};
    input.read(reinterpret_cast<char*>(prefix.data()), prefix.size());
    return input.gcount() != 0 && prefix[0] == 'P';
}

inline WarmHistory load_warm_history(const std::filesystem::path& path,
                                     const SearchSpace& space, const Json& recorded) {
    if (path.empty() || !binary_warm_file(path))
        return load_json_warm_history(path, space, recorded);
    try {
        WarmHistory history;
        auto source = std::make_shared<BinaryWarmSource>(path, space, recorded);
        history.completed = source->completed;
        history.feasible = source->feasible;
        history.sampler_state = source->sampler_state;
        history.next_id = source->id(source->size() - 1) + 1;
        history.binary = std::move(source);
        return history;
    } catch (const std::exception& error) {
        throw WarmStartError(error.what());
    }
}

template <class Integer>
inline void warm_write_integer(std::ostream& output, Integer value) {
    for (std::size_t byte = 0; byte < sizeof(value); ++byte)
        output.put(static_cast<char>(value >> (byte * 8)));
}

inline void warm_write_real(std::ostream& output, const Json& value) {
    std::uint64_t bits = warm_null;
    if (value.kind != Json::Kind::Null) {
        const auto real = value.real();
        std::memcpy(&bits, &real, sizeof(bits));
    }
    warm_write_integer(output, bits);
}

inline void encode_warm_history(const std::filesystem::path& path, const SearchSpace& space,
                                const Json& recorded, const WarmHistory& history,
                                std::uint64_t block_trials) {
    const auto temporary = path.string() + ".tmp." + std::to_string(::getpid());
    try {
        if (!history.size())
            throw std::runtime_error("binary warm input must contain at least one trial");
        std::map<std::string, Dimension> sorted;
        for (const auto& dimension : space.dimensions())
            sorted.emplace(std::string(dimension_name(dimension)), dimension);
        std::vector<WarmParameterCodec> codecs;
        for (const auto& entry : sorted)
            codecs.emplace_back(entry.second);
        const auto constraints = field(field(recorded, "objective"), "constraints").items.size();
        const auto constraint_order = warm_constraint_order(recorded);
        const auto hash = space_hash(recorded);
        if (!block_trials)
            block_trials = history.size();
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output)
            throw std::runtime_error("cannot open binary warm output");
        for (std::uint64_t begin = 0; begin < history.size();) {
            const auto count = std::min(block_trials, history.size() - begin);
            const auto header_bytes = warm_add(80, warm_multiply(4, codecs.size()));
            std::uint64_t row_bytes = warm_add(17, warm_multiply(8, constraints));
            for (const auto& codec : codecs)
                row_bytes = warm_add(row_bytes, codec.encoding == 1 ? 4 : 8);
            output.write(reinterpret_cast<const char*>(warm_magic.data()), warm_magic.size());
            warm_write_integer(output, std::uint16_t{2});
            warm_write_integer(output, std::uint16_t{0});
            warm_write_integer(output, static_cast<std::uint32_t>(header_bytes));
            warm_write_integer(output, warm_add(header_bytes, warm_multiply(count, row_bytes)));
            warm_write_integer(output, count);
            warm_write_integer(output, static_cast<std::uint32_t>(codecs.size()));
            warm_write_integer(output, std::uint32_t{1});
            warm_write_integer(output, static_cast<std::uint32_t>(constraints));
            warm_write_integer(output, std::uint32_t{0});
            for (std::size_t index = 0; index < 32; ++index)
                output.put(static_cast<char>(std::stoul(hash.substr(index * 2, 2), nullptr, 16)));
            for (const auto& codec : codecs) {
                output.put(static_cast<char>(codec.kind));
                output.put(static_cast<char>(codec.encoding));
                warm_write_integer(output, std::uint16_t{0});
            }
            for (std::uint64_t row = begin; row < begin + count; ++row)
                warm_write_integer(output, history.binary ? history.binary->id(row) :
                    history.observations[row].candidate.id);
            for (std::uint64_t row = begin; row < begin + count; ++row) {
                const auto temporary_record = history.binary ? history.record(space, row) : Json{};
                const auto& record = history.binary ? temporary_record : history.records[row];
                const auto state = field(record, "status").text();
                const auto found = std::find(warm_states.begin(), warm_states.end(), state);
                output.put(static_cast<char>(std::distance(warm_states.begin(), found)));
            }
            for (const auto& codec : codecs) {
                for (std::uint64_t row = begin; row < begin + count; ++row) {
                    const auto temporary_observation = history.binary
                        ? history.binary->observation(space, row) : WarmStartObservation{};
                    const auto& observation = history.binary
                        ? temporary_observation : history.observations[row];
                    const auto& value = observation.candidate.values.at(codec.name());
                    if (codec.encoding == 1)
                        warm_write_integer(output, codec.encode(value));
                    else
                        warm_write_real(output, scalar_json(value));
                }
            }
            for (std::uint64_t row = begin; row < begin + count; ++row) {
                const auto temporary_record = history.binary ? history.record(space, row) : Json{};
                const auto& record = history.binary ? temporary_record : history.records[row];
                warm_write_real(output, field(record, "objective"));
            }
            for (std::size_t column = 0; column < constraints; ++column) {
                for (std::uint64_t row = begin; row < begin + count; ++row) {
                    const auto temporary_record = history.binary
                        ? history.record(space, row) : Json{};
                    const auto& record = history.binary ? temporary_record : history.records[row];
                    const auto* values = record.find("constraint_values");
                    if (values && (values->kind != Json::Kind::Array ||
                                   values->items.size() != constraints))
                        throw std::runtime_error("constraint column counts do not match study");
                    warm_write_real(output, values && column < values->items.size()
                        ? values->items[constraint_order[column]] : Json{});
                }
            }
            begin += count;
        }
        if (!history.sampler_state.empty()) {
            output.write(reinterpret_cast<const char*>(warm_state_magic.data()),
                         warm_state_magic.size());
            warm_write_integer(output, static_cast<std::uint64_t>(history.sampler_state.size()));
            output.write(history.sampler_state.data(), history.sampler_state.size());
        }
        output.flush();
        if (!output)
            throw std::runtime_error("failed writing binary warm output");
        output.close();
        std::filesystem::rename(temporary, path);
    } catch (const std::exception& error) {
        std::filesystem::remove(temporary);
        throw WarmStartError(error.what());
    }
}

}
