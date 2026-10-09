// Native fixture and refusal tests for src/cli/candidate_list.hpp.
//
// Written against docs/internal/methods-c-native.md and the AR pin before the header was
// compiled. They have NOT been executed: the first build and run happens on the AWS spot
// phase. Every digest golden below was computed independently with `shasum -a 256` from the
// documented byte layout, not by the code under test.
//
// Registration for the integration lane (tests/CMakeLists.txt, next to warm_binary):
//   add_executable(pineforge_hpo_test_candidate_list test_candidate_list.cpp)
//   target_link_libraries(pineforge_hpo_test_candidate_list PRIVATE PineForgeHPO::core)
//   target_compile_options(pineforge_hpo_test_candidate_list PRIVATE -Wall -Wextra -Wpedantic)
//   add_test(NAME pineforge_hpo_candidate_list COMMAND pineforge_hpo_test_candidate_list)

#include "../src/cli/candidate_list.hpp"

#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

namespace pfh = pineforge::hpo;
namespace det = pineforge::hpo::detail;
namespace fs = std::filesystem;

namespace {

fs::path scratch;
// Tokens and paths that must never appear in a diagnostic.
std::vector<std::string> forbidden;

[[noreturn]] void fail(const std::string& message) {
    throw std::runtime_error(message);
}

void require(bool condition, const std::string& message) {
    if (!condition)
        fail(message);
}

// Bit-exact scalar identity: a double is compared by its binary64 bits, so +0.0 and -0.0 differ
// and a value cannot pass as equal merely because `==` says so.
bool same_bits(const pfh::ParameterValue& left, const pfh::ParameterValue& right) {
    if (left.index() != right.index())
        return false;
    if (const auto* real = std::get_if<double>(&left)) {
        const double other = std::get<double>(right);
        std::uint64_t first = 0;
        std::uint64_t second = 0;
        static_assert(sizeof(first) == sizeof(*real));
        std::memcpy(&first, real, sizeof(first));
        std::memcpy(&second, &other, sizeof(second));
        return first == second;
    }
    return left == right;
}

bool same_candidate_bits(const pfh::Candidate& left, const pfh::Candidate& right) {
    if (left.values.size() != right.values.size())
        return false;
    auto first = left.values.begin();
    auto second = right.values.begin();
    for (; first != left.values.end(); ++first, ++second)
        if (first->first != second->first || !same_bits(first->second, second->second))
            return false;
    return true;
}

struct Refusal {
    std::string code;
    std::string reason;
    std::string message;
};

// Runs `callable`, requires a typed HPO failure, and checks the diagnostic leaks nothing.
template <typename Callable>
Refusal refusal_of(const std::string& label, Callable&& callable) {
    try {
        callable();
    } catch (const pfh::HpoError& error) {
        Refusal refusal;
        refusal.code = error.code();
        refusal.message = error.what();
        const auto found = error.args().find("reason");
        if (found != error.args().end())
            refusal.reason = std::get<std::string>(found->second.value());
        else
            require(error.args().empty(), label + ": unexpected failure arguments");
        for (const auto& token : forbidden)
            require(refusal.message.find(token) == std::string::npos,
                    label + ": diagnostic leaked a caller token or path: " + refusal.message);
        return refusal;
    }
    fail(label + ": input that must be refused was accepted");
}

// `reason` is empty for codes that carry no arguments.
template <typename Callable>
void expect_refusal(const std::string& label, const char* code, const char* reason,
                    Callable&& callable, const std::string& mention = {}) {
    const auto refusal = refusal_of(label, std::forward<Callable>(callable));
    require(refusal.code == code,
            label + ": wrong failure code " + refusal.code + " (" + refusal.message + ")");
    require(refusal.reason == reason,
            label + ": wrong failure reason '" + refusal.reason + "' (" + refusal.message + ")");
    if (!mention.empty())
        require(refusal.message.find(mention) != std::string::npos,
                label + ": diagnostic lacks '" + mention + "': " + refusal.message);
}

// ---- search spaces ------------------------------------------------------------------

pfh::SearchSpace golden_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(pfh::IntegerDimension("Length", 2, 40, 2));
    dimensions.emplace_back(pfh::RealDimension("Level", -10.0, 10.0));
    dimensions.emplace_back(pfh::BooleanDimension("Fast"));
    dimensions.emplace_back(
        pfh::CategoricalDimension("Mode", {std::string("fast"), std::string("slow")}));
    return pfh::SearchSpace(std::move(dimensions));
}

// Continuous reals make the space non-finite: validated, not canonicalized.
pfh::SearchSpace mixed_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(pfh::IntegerDimension("Length", 2, 40, 2));
    dimensions.emplace_back(pfh::RealDimension("Level", -10.0, 10.0));
    dimensions.emplace_back(pfh::BooleanDimension("Fast"));
    dimensions.emplace_back(
        pfh::CategoricalDimension("Mode", {std::string("fast"), std::string("slow")}));
    dimensions.emplace_back(pfh::CategoricalDimension("Bucket", {0.5, 1.0, 2.0}));
    dimensions.emplace_back(pfh::CategoricalDimension(
        "Pick", {std::int64_t{1}, std::int64_t{2}, std::int64_t{3}}));
    dimensions.emplace_back(pfh::RealDimension("Scale", 0.001, 1000.0, std::nullopt, true));
    dimensions.emplace_back(pfh::RealDimension("Mult", 0.0, 1.0, 0.25));
    return pfh::SearchSpace(std::move(dimensions));
}

// Every dimension is on a lattice: vectors are canonicalized through candidate_ordinal.
pfh::SearchSpace finite_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(pfh::IntegerDimension("Length", 2, 40, 2));
    dimensions.emplace_back(pfh::RealDimension("Mult", 0.0, 1.0, 0.25));
    dimensions.emplace_back(pfh::BooleanDimension("Fast"));
    dimensions.emplace_back(
        pfh::CategoricalDimension("Mode", {std::string("fast"), std::string("slow")}));
    return pfh::SearchSpace(std::move(dimensions));
}

// finite_cardinality() overflows uint64: validated, not canonicalized.
pfh::SearchSpace huge_space() {
    std::vector<pfh::Dimension> dimensions;
    for (const char* name : {"A", "B", "C", "D", "E"})
        dimensions.emplace_back(pfh::IntegerDimension(name, 1, 100000, 1));
    return pfh::SearchSpace(std::move(dimensions));
}

// A continuous real and a real categorical that contains zero: a spelled -0.0 is kept as is.
pfh::SearchSpace zero_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(pfh::RealDimension("X", -1.0, 1.0));
    dimensions.emplace_back(pfh::CategoricalDimension("Z", {0.0, 1.5}));
    return pfh::SearchSpace(std::move(dimensions));
}

// The finite twin: the importer's lattice canonicalization decides the stored zero.
pfh::SearchSpace zero_lattice_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(pfh::RealDimension("X", -1.0, 1.0, 0.5));
    dimensions.emplace_back(pfh::CategoricalDimension("Z", {0.0, 1.5}));
    return pfh::SearchSpace(std::move(dimensions));
}

pfh::SearchSpace tiny_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(
        pfh::CategoricalDimension("Mode", {std::string("fast"), std::string("slow")}));
    return pfh::SearchSpace(std::move(dimensions));
}

pfh::SearchSpace blob_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(
        pfh::CategoricalDimension("Blob", {std::string("s"), std::string(60000, 'x')}));
    return pfh::SearchSpace(std::move(dimensions));
}

pfh::SearchSpace name_space() {
    std::vector<pfh::Dimension> dimensions;
    dimensions.emplace_back(pfh::CategoricalDimension(
        "Name", {std::string("caf\xc3\xa9"), std::string("\xf0\x9f\x98\x80")}));
    return pfh::SearchSpace(std::move(dimensions));
}

// ---- line construction --------------------------------------------------------------

using Tokens = std::vector<std::pair<std::string, std::string>>;

std::string render(const Tokens& tokens) {
    std::string out = "{";
    for (std::size_t index = 0; index < tokens.size(); ++index) {
        if (index != 0)
            out += ',';
        out += '"' + tokens[index].first + "\":" + tokens[index].second;
    }
    return out + "}";
}

Tokens with(Tokens tokens, const std::string& key, const std::string& token) {
    for (auto& entry : tokens) {
        if (entry.first == key) {
            entry.second = token;
            return tokens;
        }
    }
    tokens.emplace_back(key, token);
    return tokens;
}

Tokens without(Tokens tokens, const std::string& key) {
    tokens.erase(std::remove_if(tokens.begin(), tokens.end(),
                                [&](const auto& entry) { return entry.first == key; }),
                 tokens.end());
    return tokens;
}

Tokens golden_tokens() {
    return {{"Length", "4"}, {"Level", "0.5"}, {"Fast", "true"}, {"Mode", "\"slow\""}};
}

Tokens mixed_tokens() {
    return {{"Length", "4"}, {"Level", "1.5"}, {"Fast", "true"}, {"Mode", "\"fast\""},
            {"Bucket", "0.5"}, {"Pick", "2"}, {"Scale", "0.1"}, {"Mult", "0.25"}};
}

Tokens finite_tokens() {
    return {{"Length", "4"}, {"Mult", "0.25"}, {"Fast", "true"}, {"Mode", "\"fast\""}};
}

std::string golden_line(const std::string& key, const std::string& token) {
    return render(with(golden_tokens(), key, token)) + "\n";
}

void write_file(const fs::path& path, const std::string& bytes) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    output.flush();
    require(output.good(), "cannot write a test fixture");
}

std::string tiny_lines(std::uint64_t count) {
    std::string text;
    text.reserve(static_cast<std::size_t>(count) * 16);
    for (std::uint64_t index = 0; index < count; ++index)
        text += "{\"Mode\":\"fast\"}\n";
    return text;
}

// Exactly `target` bytes of valid blob_space lines (the last line is padded with spaces).
std::string blob_text(std::uint64_t target) {
    const std::string full = "{\"Blob\":\"" + std::string(60000, 'x') + "\"}\n";
    const std::string small = "{\"Blob\":\"s\"}";
    std::string text;
    while (text.size() + full.size() + small.size() + 1 <= target)
        text += full;
    const std::size_t remaining = static_cast<std::size_t>(target) - text.size();
    text += small;
    text += std::string(remaining - small.size() - 1, ' ');
    text += '\n';
    require(text.size() == target, "blob fixture has the wrong size");
    return text;
}

// ---- goldens (independent shasum -a 256 values) -------------------------------------

const std::string golden_text =
    "{\"Length\":4,\"Level\":0.5,\"Fast\":true,\"Mode\":\"slow\"}\n"
    "{\"Mode\":\"fast\",\"Fast\":false,\"Level\":-1.25,\"Length\":6}\n"
    "{\"Length\":4,\"Level\":0.5,\"Fast\":true,\"Mode\":\"slow\"}\n";
// The same three vectors with other spacing, key order, number spelling and CRLF, no final LF.
const std::string golden_variant =
    "{ \"Mode\" : \"slow\", \"Fast\":true, \"Level\":5E-1, \"Length\":4 }\r\n"
    "{\"Length\":6,\"Level\":-125e-2,\"Fast\":false,\"Mode\":\"fast\"}\r\n"
    "  {\"Level\":5.0e-1,\"Length\":4,\"Fast\":true,\"Mode\":\"slow\"}";
const std::string golden_pair =
    "{\"Length\":4,\"Level\":0.5,\"Fast\":true,\"Mode\":\"slow\"}\n"
    "{\"Mode\":\"fast\",\"Fast\":false,\"Level\":-1.25,\"Length\":6}\n";
// Canonical keys: {"Fast":["boolean",true],"Length":["integer","4"],"Level":["real",
// "3fe0000000000000"],"Mode":["string","slow"]} and the -1.25 / fast / 6 / false analogue,
// each followed by LF after the line `pineforge_candidates_v1`.
const std::string golden_source =
    "6dcbe37913f3114901aad4e669db3562f57234e0ba84ba89232f1e985fd8e574";
const std::string golden_variant_source =
    "56d26f0f694fa01250a37aa58af66f906da1979632697868729d15f1e41e89d6";
const std::string golden_list =
    "1d459a9471d29dbb6a0bf8b573491d2d919dcf38a201ae7fe29792e05a2fbc1e";
const std::string golden_pair_list =
    "03d32f86ca9e5cb757f1fcc577511de02b158ea0df027f548a5d9d37c326b92c";

// ---- tests --------------------------------------------------------------------------

void golden_order_and_duplicates() {
    const auto space = golden_space();
    const auto list = det::parse_candidate_list(golden_text, space);
    require(list.size() == 3, "three occurrences expected, duplicates must be kept");
    require(list.source_sha256() == golden_source, "source digest differs from the golden");
    require(list.list_sha256() == golden_list, "list digest differs from the golden");
    const auto first = list.at(0);
    const auto second = list.at(1);
    const auto third = list.at(2);
    for (std::uint64_t position = 0; position < 3; ++position)
        require(list.at(position).id == position, "trial id must equal the zero-based position");
    require(first.values == third.values, "duplicate occurrences must keep identical vectors");
    require(first.values != second.values, "distinct occurrences were merged");
    require(std::get<std::int64_t>(first.values.at("Length")) == 4, "Length decoded wrongly");
    require(std::get<double>(first.values.at("Level")) == 0.5, "Level decoded wrongly");
    require(std::get<bool>(first.values.at("Fast")), "Fast decoded wrongly");
    require(std::get<std::string>(first.values.at("Mode")) == "slow", "Mode decoded wrongly");
    require(std::get<std::int64_t>(second.values.at("Length")) == 6, "second Length wrong");
    require(std::get<double>(second.values.at("Level")) == -1.25, "second Level wrong");
    require(!std::get<bool>(second.values.at("Fast")), "second Fast wrong");
    require(std::get<std::string>(second.values.at("Mode")) == "fast", "second Mode wrong");

    det::CandidateListCursor cursor(list);
    for (std::uint64_t position = 0; position < 3; ++position) {
        const auto next = cursor.next();
        require(next.has_value() && next->id == position &&
                    next->values == list.at(position).values,
                "the cursor must yield occurrences in list order with positional ids");
        require(cursor.position() == position + 1, "cursor position does not advance");
    }
    require(!cursor.next().has_value() && !cursor.next().has_value() && cursor.position() == 3,
            "an exhausted cursor must stay exhausted");
    expect_refusal("position past the end", "hpo_invariant", "", [&] { (void)list.at(3); });

    // Whitespace, key order, number spelling, CRLF and the final LF do not change the list
    // digest; they do change the source digest.
    const auto spelled = det::parse_candidate_list(golden_variant, space);
    require(spelled.size() == 3, "variant spelling changed the occurrence count");
    require(spelled.list_sha256() == golden_list, "list digest depends on spelling");
    require(spelled.source_sha256() == golden_variant_source, "variant source digest differs");
    require(spelled.source_sha256() != list.source_sha256(), "source digest ignored the bytes");
    for (std::uint64_t position = 0; position < 3; ++position)
        require(spelled.at(position).values == list.at(position).values,
                "variant spelling changed a canonical vector");

    // Multiplicity and order are part of the identity.
    const auto pair = det::parse_candidate_list(golden_pair, space);
    require(pair.size() == 2 && pair.list_sha256() == golden_pair_list,
            "two-vector digest differs from the golden");
    require(pair.list_sha256() != list.list_sha256(), "dropping a duplicate kept the digest");
    const auto swapped = det::parse_candidate_list(
        "{\"Mode\":\"fast\",\"Fast\":false,\"Level\":-1.25,\"Length\":6}\n" +
            golden_line("Length", "4") + golden_line("Length", "4"),
        space);
    require(swapped.size() == 3 && swapped.list_sha256() != list.list_sha256(),
            "reordering the occurrences kept the digest");
}

void canonical_spellings() {
    const auto space = golden_space();
    std::optional<std::string> reference;
    for (const char* token : {"5", "5.0", "5e0", "0.5e1", "50E-1", "5.00", "5E+0"}) {
        const auto list = det::parse_candidate_list(golden_line("Level", token), space);
        require(std::get<double>(list.at(0).values.at("Level")) == 5.0,
                std::string("Level spelling ") + token + " did not decode to 5.0");
        if (!reference)
            reference = list.list_sha256();
        require(list.list_sha256() == *reference,
                std::string("Level spelling ") + token + " changed the list digest");
    }
    // A continuous real keeps exactly what the importer decodes, the sign bit of a zero
    // included. The list digest ignores that sign (candidate_key), the source digest does not.
    std::optional<std::string> zero;
    std::optional<std::string> positive_source;
    for (const char* token : {"0", "0.0", "-0", "-0.0", "0e5", "-0e0", "-0.0e-3"}) {
        const auto list = det::parse_candidate_list(golden_line("Level", token), space);
        const double level = std::get<double>(list.at(0).values.at("Level"));
        require(level == 0.0 && std::signbit(level) == (token[0] == '-'),
                std::string("zero spelling ") + token + " lost or invented a sign bit");
        if (!zero)
            zero = list.list_sha256();
        require(list.list_sha256() == *zero,
                std::string("zero spelling ") + token + " changed the list digest");
        if (token[0] != '-' && !positive_source)
            positive_source = list.source_sha256();
        if (token[0] == '-')
            require(list.source_sha256() != *positive_source,
                    std::string("zero spelling ") + token + " did not change the source digest");
    }

    // A real categorical choice accepts an integer spelling (importer fallback) and is stored
    // as the real; an integer choice does not accept a real spelling.
    const auto mixed = mixed_space();
    const auto bucket = det::parse_candidate_list(
        render(with(mixed_tokens(), "Bucket", "1")) + "\n", mixed);
    require(std::holds_alternative<double>(bucket.at(0).values.at("Bucket")) &&
                std::get<double>(bucket.at(0).values.at("Bucket")) == 1.0,
            "integer spelling of a real choice was not stored as the real");
    const auto bucket_real = det::parse_candidate_list(
        render(with(mixed_tokens(), "Bucket", "1.0")) + "\n", mixed);
    require(bucket.list_sha256() == bucket_real.list_sha256(),
            "1 and 1.0 on a real categorical choice must hash alike");
    expect_refusal("real spelling of an int choice", "hpo_study_spec_invalid", "search_space",
                   [&] {
                       (void)det::parse_candidate_list(
                           render(with(mixed_tokens(), "Pick", "1.0")) + "\n", mixed);
                   }, "Pick");

    // UTF-8: raw bytes and JSON escapes denote the same strings.
    const auto names = name_space();
    const auto raw = det::parse_candidate_list("{\"Name\":\"caf\xc3\xa9\"}\n", names);
    const auto escaped = det::parse_candidate_list("{\"Name\":\"caf\\u00e9\"}\n", names);
    require(raw.list_sha256() == escaped.list_sha256() &&
                raw.source_sha256() != escaped.source_sha256(),
            "UTF-8 and \\u escapes must give one canonical list and two source digests");
    require(std::get<std::string>(raw.at(0).values.at("Name")) == "caf\xc3\xa9",
            "UTF-8 string decoded wrongly");
    const auto astral_raw = det::parse_candidate_list("{\"Name\":\"\xf0\x9f\x98\x80\"}\n", names);
    const auto astral_escaped =
        det::parse_candidate_list("{\"Name\":\"\\ud83d\\ude00\"}\n", names);
    require(astral_raw.list_sha256() == astral_escaped.list_sha256(),
            "a surrogate-pair escape must equal the raw four-byte sequence");
}

void invalid_vectors() {
    const auto space = golden_space();
    const auto refuse = [&](const std::string& label, const std::string& text,
                            const std::string& mention) {
        expect_refusal(label, "hpo_study_spec_invalid", "search_space",
                       [&] { (void)det::parse_candidate_list(text, space); }, mention);
    };
    refuse("missing key", render(without(golden_tokens(), "Fast")) + "\n", "line 1");
    refuse("missing key names the dimension", render(without(golden_tokens(), "Fast")) + "\n",
           "Fast");
    refuse("missing key on line 3",
           golden_line("Length", "4") + golden_line("Length", "6") +
               render(without(golden_tokens(), "Mode")) + "\n", "line 3");
    refuse("unknown key", render(with(golden_tokens(), "PFSENTINEL_KEY", "1")) + "\n",
           "not a search dimension");
    refuse("key that names only a fixed input",
           render(with(golden_tokens(), "BatchPrefixTest", "1")) + "\n",
           "not a search dimension");
    refuse("string for int", golden_line("Length", "\"4\""), "expected integer");
    refuse("real for int", golden_line("Length", "4.0"), "expected integer");
    refuse("exponent for int", golden_line("Length", "4e0"), "expected integer");
    refuse("bool for int", golden_line("Length", "true"), "expected integer");
    refuse("null for int", golden_line("Length", "null"), "expected integer");
    refuse("array for int", golden_line("Length", "[4]"), "expected integer");
    refuse("object for int", golden_line("Length", "{}"), "expected integer");
    refuse("int64 overflow", golden_line("Length", "9223372036854775808"), "expected integer");
    refuse("int64 underflow", golden_line("Length", "-9223372036854775809"), "expected integer");
    refuse("off-lattice int", golden_line("Length", "3"), "outside the declared domain");
    refuse("int below low", golden_line("Length", "0"), "outside the declared domain");
    refuse("int above high", golden_line("Length", "42"), "outside the declared domain");
    refuse("int for bool", golden_line("Fast", "1"), "expected boolean");
    refuse("string for bool", golden_line("Fast", "\"true\""), "expected boolean");
    refuse("unknown choice", golden_line("Mode", "\"PFSENTINEL_VALUE\""),
           "outside the declared domain");
    refuse("int for string choice", golden_line("Mode", "1"), "outside the declared domain");
    refuse("bool for string choice", golden_line("Mode", "true"), "outside the declared domain");
    refuse("string for real", golden_line("Level", "\"0.5\""), "expected real");
    refuse("bool for real", golden_line("Level", "true"), "expected real");
    refuse("null for real", golden_line("Level", "null"), "expected real");
    refuse("real overflow", golden_line("Level", "1e999"), "expected real");
    refuse("real above high", golden_line("Level", "10.5"), "outside the declared domain");
    refuse("real below low", golden_line("Level", "-10.5"), "outside the declared domain");
    for (const char* text : {"[]\n", "5\n", "\"PFSENTINEL_VALUE\"\n", "null\n", "true\n"})
        refuse(std::string("non-object line ") + (text[0] == '"' ? "string" : text),
               text, "must be a JSON object");

    // A fixed-input-only key is refused even when every dimension is present and valid.
    expect_refusal("empty list", "hpo_study_spec_invalid", "sampler",
                   [&] { (void)det::parse_candidate_list("", space); }, "empty");
}

void json_and_utf8() {
    const auto space = golden_space();
    const std::string good = render(golden_tokens());
    const auto refuse = [&](const std::string& label, const std::string& text,
                            const std::string& mention) {
        expect_refusal(label, "hpo_input_file_invalid", "",
                       [&] { (void)det::parse_candidate_list(text, space); }, mention);
    };
    refuse("blank line in the middle", good + "\n\n" + good + "\n", "line 2");
    refuse("blank line at the end", good + "\n\n", "line 2");
    refuse("whitespace-only line", "   \n", "line 1");
    refuse("single LF", "\n", "line 1");
    refuse("trailing garbage", good + " x\n", "not valid JSON");
    refuse("two objects on one line", good + good + "\n", "not valid JSON");
    refuse("comment line", "// header\n" + good + "\n", "line 1");
    refuse("trailing comma", "{\"Length\":4,}\n", "not valid JSON");
    refuse("single quotes", "{'Length':4}\n", "not valid JSON");
    refuse("unterminated string", "{\"Length\":4,\"Mode\":\"slow}\n", "not valid JSON");
    for (const char* token : {"NaN", "Infinity", "-Infinity", "+1", ".5", "1.", "01", "0x10",
                              "1e", "1e+", "--1", "- 1"})
        refuse(std::string("number grammar ") + token, golden_line("Level", token),
               "not valid JSON");
    refuse("duplicate unknown key", "{\"PFSENTINEL_KEY\":1,\"PFSENTINEL_KEY\":2}\n",
           "not valid JSON");
    refuse("duplicate declared key",
           "{\"Length\":4,\"Length\":6,\"Level\":0.5,\"Fast\":true,\"Mode\":\"slow\"}\n",
           "not valid JSON");
    refuse("nesting deeper than 32",
           "{\"Length\":" + std::string(40, '[') + std::string(40, ']') +
               ",\"Level\":0.5,\"Fast\":true,\"Mode\":\"slow\"}\n", "not valid JSON");
    refuse("byte order mark", "\xef\xbb\xbf" + good + "\n", "line 1");
    refuse("NUL after the value", good + std::string(1, '\0') + "\n", "not valid JSON");
    refuse("raw control character in a string", golden_line("Mode", "\"sl\x01ow\""),
           "not valid JSON");
    refuse("lone high surrogate escape", golden_line("Mode", "\"\\ud800\""), "not valid JSON");
    refuse("lone low surrogate escape", golden_line("Mode", "\"\\udc00\""), "not valid JSON");
    refuse("high surrogate then non-surrogate", golden_line("Mode", "\"\\ud800\\u0041\""),
           "not valid JSON");
    refuse("unknown escape", golden_line("Mode", "\"\\q\""), "not valid JSON");
    const std::vector<std::pair<const char*, std::string>> bad_utf8{
        {"0xFF", "\xff"}, {"overlong NUL", "\xc0\x80"}, {"overlong slash", "\xc1\xbf"},
        {"overlong 3-byte", "\xe0\x80\x80"}, {"UTF-16 surrogate", "\xed\xa0\x80"},
        {"above U+10FFFF", "\xf4\x90\x80\x80"}, {"0xF5 lead", "\xf5\x80\x80\x80"},
        {"truncated sequence", "\xe2\x82"}, {"stray continuation", "\x80"},
        {"bad continuation", "\xc3\x28"}};
    for (const auto& item : bad_utf8)
        refuse(std::string("invalid UTF-8: ") + item.first,
               golden_line("Mode", "\"" + item.second + "\""), "not valid UTF-8");
    refuse("invalid UTF-8 in a key", "{\"\xff\":1}\n", "not valid UTF-8");
    refuse("invalid UTF-8 between tokens", good + "\xa0\n", "not valid UTF-8");
    // CRLF line ends are JSON whitespace and are accepted (see golden_variant).
    const auto crlf = det::parse_candidate_list(good + "\r\n" + good + "\r\n", space);
    require(crlf.size() == 2, "CRLF lines must be accepted as JSON whitespace");
}

void limits() {
    // Occurrence count: exactly 50,000 passes, 50,001 is refused, with and without final LF.
    const auto tiny = tiny_space();
    {
        auto text = tiny_lines(det::candidate_list_max_occurrences);
        const auto list = det::parse_candidate_list(text, tiny);
        require(list.size() == 50000, "50,000 occurrences must be admitted");
        require(list.at(49999).id == 49999, "the last occurrence has the wrong id");
        text.pop_back();
        require(det::parse_candidate_list(text, tiny).size() == 50000,
                "a missing final LF changed the count");
        expect_refusal("50,001 occurrences", "hpo_study_spec_invalid", "sampler",
                       [&] { (void)det::parse_candidate_list(tiny_lines(50001), tiny); },
                       "50000");
    }
    // Line length: exactly 65,536 bytes passes, one more is refused.
    {
        const std::string head = "{\"Mode\":\"fast\"}";
        const std::string exact = head + std::string(65536 - head.size(), ' ') + "\n";
        require(det::parse_candidate_list(exact, tiny).size() == 1,
                "a 65,536-byte line must be admitted");
        const std::string over = head + std::string(65537 - head.size(), ' ') + "\n";
        expect_refusal("65,537-byte line", "hpo_input_file_invalid", "",
                       [&] { (void)det::parse_candidate_list(over, tiny); }, "line 1");
        expect_refusal("over-long second line", "hpo_input_file_invalid", "",
                       [&] { (void)det::parse_candidate_list(head + "\n" + over, tiny); },
                       "line 2");
    }
    // File size: exactly 32 MiB passes, one byte more is refused (memory and file routes).
    {
        const auto blobs = blob_space();
        const std::uint64_t limit = det::candidate_list_max_file_bytes;
        auto exact = blob_text(limit);
        const auto expected_lines =
            static_cast<std::uint64_t>(std::count(exact.begin(), exact.end(), '\n'));
        const auto path = scratch / "exact.jsonl";
        write_file(path, exact);
        const auto from_file = det::load_candidate_list(path, blobs);
        require(from_file.size() == expected_lines, "the 32 MiB file lost occurrences");
        require(det::parse_candidate_list(std::move(exact), blobs).list_sha256() ==
                    from_file.list_sha256(),
                "memory and file admission disagree");
        auto over = blob_text(limit + 1);
        const auto over_path = scratch / "over.jsonl";
        write_file(over_path, over);
        expect_refusal("32 MiB + 1 from a file", "hpo_input_file_invalid", "",
                       [&] { (void)det::load_candidate_list(over_path, blobs); }, "bytes");
        expect_refusal("32 MiB + 1 from memory", "hpo_input_file_invalid", "",
                       [&] { (void)det::parse_candidate_list(std::move(over), blobs); },
                       "bytes");
    }
    // Callers may tighten the limits; they can never raise them.
    {
        const det::CandidateListLimits tight{3, 100, 40};
        const std::string line = "{\"Mode\":\"fast\"}\n";
        require(det::parse_candidate_list(line + line + line, tiny, {}, tight).size() == 3,
                "tight occurrence limit rejected a list at the limit");
        expect_refusal("tight occurrences", "hpo_study_spec_invalid", "sampler",
                       [&] { (void)det::parse_candidate_list(line + line + line + line, tiny, {},
                                                             tight); }, "3 occurrences");
        expect_refusal("tight line", "hpo_input_file_invalid", "", [&] {
            (void)det::parse_candidate_list("{\"Mode\":\"fast\"}" + std::string(30, ' ') + "\n",
                                            tiny, {}, tight); }, "line 1");
        expect_refusal("tight file", "hpo_input_file_invalid", "", [&] {
            (void)det::parse_candidate_list(std::string(101, ' '), tiny, {}, tight); }, "bytes");
        for (const det::CandidateListLimits& bad :
             {det::CandidateListLimits{0, 100, 40}, det::CandidateListLimits{3, 0, 40},
              det::CandidateListLimits{3, 100, 0}, det::CandidateListLimits{50001, 100, 40},
              det::CandidateListLimits{3, 32ULL * 1024 * 1024 + 1, 40},
              det::CandidateListLimits{3, 100, 65537}})
            expect_refusal("limits above the maxima", "hpo_invariant", "",
                           [&] { (void)det::parse_candidate_list(line, tiny, {}, bad); });
    }
}

void paths_and_file_types() {
    const auto space = golden_space();
    const auto refuse = [&](const std::string& label, const fs::path& path,
                            const std::string& mention) {
        expect_refusal(label, "hpo_input_file_invalid", "",
                       [&] { (void)det::load_candidate_list(path, space); }, mention);
    };
    const auto good = scratch / "good.jsonl";
    write_file(good, golden_text);
    const auto direct = det::load_candidate_list(good, space);
    require(direct.size() == 3 && direct.source_sha256() == golden_source &&
                direct.list_sha256() == golden_list,
            "a regular file must give the golden digests");

    refuse("empty path", fs::path(), "path is empty");
    refuse("missing file", scratch / "missing.jsonl", "cannot be opened");
    refuse("directory", scratch, "not a regular file");
    refuse("embedded NUL in the path", fs::path(std::string("a\0b", 3)), "path is invalid");
    refuse("/dev/null", fs::path("/dev/null"), "not a regular file");
    refuse("/dev/zero", fs::path("/dev/zero"), "not a regular file");

    const auto fifo = scratch / "queue.fifo";
    require(::mkfifo(fifo.c_str(), 0600) == 0, "cannot create a FIFO fixture");
    // Must return at once: open(2) uses O_NONBLOCK and the type is checked before any read.
    refuse("FIFO", fifo, "not a regular file");

    const auto link = scratch / "link.jsonl";
    fs::create_symlink(good, link);
    const auto through = det::load_candidate_list(link, space);
    require(through.source_sha256() == golden_source && through.list_sha256() == golden_list,
            "a symlink to a regular file resolves to the same bytes");
    const auto dir_link = scratch / "dir-link";
    fs::create_symlink(scratch, dir_link);
    refuse("symlink to a directory", dir_link, "not a regular file");
    const auto dangling = scratch / "dangling.jsonl";
    fs::create_symlink(scratch / "nowhere.jsonl", dangling);
    refuse("dangling symlink", dangling, "cannot be opened");

    const auto empty = scratch / "empty.jsonl";
    write_file(empty, "");
    expect_refusal("zero-byte file", "hpo_study_spec_invalid", "sampler",
                   [&] { (void)det::load_candidate_list(empty, space); }, "empty");
    const auto invalid = scratch / "invalid.jsonl";
    write_file(invalid, golden_line("Length", "3"));
    expect_refusal("invalid vector in a file", "hpo_study_spec_invalid", "search_space",
                   [&] { (void)det::load_candidate_list(invalid, space); }, "line 1");

    if (::geteuid() != 0) {
        const auto locked = scratch / "locked.jsonl";
        write_file(locked, golden_text);
        fs::permissions(locked, fs::perms::none);
        refuse("unreadable file", locked, "cannot be opened");
    }
}

void fixed_inputs() {
    const auto space = golden_space();
    const std::map<std::string, std::string> overlap{{"Length", "5"}};
    // A configuration conflict is reported before any byte of the list is looked at.
    expect_refusal("fixed input equals a dimension", "hpo_study_spec_invalid", "input",
                   [&] { (void)det::parse_candidate_list(golden_text, space, overlap); },
                   "Length");
    expect_refusal("conflict wins over a bad list", "hpo_study_spec_invalid", "input",
                   [&] { (void)det::parse_candidate_list("not json\n", space, overlap); });
    expect_refusal("conflict wins over a missing file", "hpo_study_spec_invalid", "input",
                   [&] { (void)det::load_candidate_list(scratch / "missing.jsonl", space,
                                                        overlap); });
    // A fixed input with another name is fine, and naming it in the list is an unknown key.
    const std::map<std::string, std::string> other{{"BatchPrefixTest", "1"}};
    require(det::parse_candidate_list(golden_text, space, other).size() == 3,
            "a non-overlapping fixed input must be accepted");
    expect_refusal("list names a fixed input", "hpo_study_spec_invalid", "search_space",
                   [&] { (void)det::parse_candidate_list(
                             render(with(golden_tokens(), "BatchPrefixTest", "1")) + "\n", space,
                             other); }, "not a search dimension");
}

// ---- parity with the warm-start importer --------------------------------------------

// The candidate the real warm-start importer builds from a one-row ok JSONL parent, or
// nullopt when the importer refuses the row.
std::optional<pfh::Candidate> importer_candidate(const pfh::SearchSpace& space,
                                                 const std::string& line) {
    const auto recorded = det::recorded_space(space, "net_profit", "maximize", {});
    auto record = det::object_json();
    record.members["trial_id"] = det::Json::number("0");
    record.members["status"] = det::Json::string("ok");
    record.members["feasible"] = det::Json::boolean(true);
    record.members["objective"] = det::Json::number("1.0");
    record.members["parameters"] = det::parse_json(line);
    record.members["space"] = recorded;
    const auto path = scratch / "parity.jsonl";
    write_file(path, det::dump_json(record) + "\n");
    try {
        const auto history = det::load_json_warm_history(path, space, recorded);
        require(history.observations.size() == 1, "the importer must read exactly one row");
        return history.observations.front().candidate;
    } catch (const det::WarmStartError&) {
        return std::nullopt;
    }
}

// The occurrence the candidate list admits for the same line, or nullopt when it refuses.
std::optional<pfh::Candidate> list_candidate(const pfh::SearchSpace& space,
                                             const std::string& line) {
    try {
        return det::parse_candidate_list(line + "\n", space).at(0);
    } catch (const pfh::HpoError& error) {
        require(error.code() == "hpo_study_spec_invalid" &&
                    std::get<std::string>(error.args().at("reason").value()) == "search_space",
                "a valid-JSON vector must only be refused as a search_space problem");
        return std::nullopt;
    }
}

struct ParityCase {
    const char* key;
    const char* token;
    int expected;  // 1 accepted, 0 refused, -1 only parity is asserted
};

// C admits a vector exactly when the importer accepts it as a one-row ok JSONL parent, so
// every admitted list can be re-imported.
void check_parity(const std::string& label, const pfh::SearchSpace& space, const Tokens& base,
                  const std::vector<ParityCase>& cases) {
    const auto check = [&](const std::string& name, const std::string& line, int expected) {
        const auto imported = importer_candidate(space, line);
        const auto listed = list_candidate(space, line);
        require(imported.has_value() == listed.has_value(),
                label + " / " + name + ": admission and importer disagree");
        if (imported)
            require(same_candidate_bits(*imported, *listed),
                    label + " / " + name +
                        ": accepted values differ from the importer's, sign bits included");
        if (expected >= 0)
            require(listed.has_value() == (expected == 1),
                    label + " / " + name + ": unexpected verdict");
    };
    check("baseline", render(base), 1);
    for (const auto& item : cases)
        check(std::string(item.key) + "=" + item.token, render(with(base, item.key, item.token)),
              item.expected);
}

void importer_parity() {
    check_parity("mixed", mixed_space(), mixed_tokens(), {
        {"Length", "4", 1}, {"Length", "2", 1}, {"Length", "40", 1}, {"Length", "3", 0},
        {"Length", "0", 0}, {"Length", "42", 0}, {"Length", "4.0", 0}, {"Length", "4e0", 0},
        {"Length", "\"4\"", 0}, {"Length", "true", 0}, {"Length", "null", 0},
        {"Length", "[4]", 0}, {"Length", "9223372036854775808", 0}, {"Length", "-4", 0},
        {"Level", "1.5", 1}, {"Level", "5", 1}, {"Level", "-10", 1}, {"Level", "10.0", 1},
        {"Level", "1e1", 1}, {"Level", "-0.0", 1}, {"Level", "0", 1}, {"Level", "-0", 1},
        {"Level", "-0e0", 1}, {"Level", "-0.0e-5", 1}, {"Level", "0.0", 1},
        {"Level", "10.000000000000002", -1}, {"Level", "10.5", 0}, {"Level", "-10.5", 0},
        {"Level", "\"1.5\"", 0}, {"Level", "true", 0}, {"Level", "null", 0},
        {"Level", "1e999", 0}, {"Level", "-1e999", 0},
        {"Fast", "true", 1}, {"Fast", "false", 1}, {"Fast", "1", 0}, {"Fast", "0", 0},
        {"Fast", "\"true\"", 0}, {"Fast", "null", 0},
        {"Mode", "\"fast\"", 1}, {"Mode", "\"slow\"", 1}, {"Mode", "\"FAST\"", 0},
        {"Mode", "1", 0}, {"Mode", "true", 0}, {"Mode", "\"\"", 0},
        {"Bucket", "0.5", 1}, {"Bucket", "1", 1}, {"Bucket", "1.0", 1}, {"Bucket", "2", 1},
        {"Bucket", "1e0", 1}, {"Bucket", "3", 0}, {"Bucket", "\"1.0\"", 0},
        {"Bucket", "true", 0}, {"Bucket", "0.75", 0},
        {"Pick", "1", 1}, {"Pick", "3", 1}, {"Pick", "1.0", 0}, {"Pick", "4", 0},
        {"Pick", "\"1\"", 0}, {"Pick", "2e0", 0},
        {"Scale", "0.1", 1}, {"Scale", "1000", 1}, {"Scale", "0.001", 1}, {"Scale", "0", 0},
        {"Scale", "-1", 0}, {"Scale", "0.0009", 0}, {"Scale", "1000.1", 0},
        {"Mult", "0.25", 1}, {"Mult", "0", 1}, {"Mult", "1", 1}, {"Mult", "0.5", 1},
        {"Mult", "0.3", 0}, {"Mult", "0.26", 0}, {"Mult", "1.25", 0}, {"Mult", "-0.25", 0},
        {"Mult", "0.25000000000000006", -1}, {"Mult", "-0.0", -1}});

    check_parity("finite", finite_space(), finite_tokens(), {
        {"Length", "40", 1}, {"Length", "3", 0}, {"Length", "4.0", 0},
        {"Mult", "0", 1}, {"Mult", "1", 1}, {"Mult", "0.75", 1}, {"Mult", "0.3", 0},
        {"Mult", "0.25000000000000006", -1}, {"Mult", "-0.0", -1},
        {"Mult", "1.0000000000000002", -1},
        {"Fast", "false", 1}, {"Fast", "0", 0}, {"Mode", "\"slow\"", 1}, {"Mode", "\"x\"", 0}});

    check_parity("huge", huge_space(),
                 {{"A", "1"}, {"B", "2"}, {"C", "3"}, {"D", "4"}, {"E", "5"}}, {
        {"A", "100000", 1}, {"A", "0", 0}, {"A", "100001", 0}, {"B", "2.0", 0},
        {"E", "\"5\"", 0}});

    // Structural cases: both sides see a missing key and an extra key alike.
    const auto space = mixed_space();
    const auto structural = [&](const std::string& name, const std::string& line) {
        require(!importer_candidate(space, line) && !list_candidate(space, line),
                name + ": admission and importer disagree");
    };
    structural("missing key", render(without(mixed_tokens(), "Mult")));
    structural("extra key", render(with(mixed_tokens(), "Extra", "1")));
    structural("empty object", "{}");

    // A finite space canonicalizes through candidate_at: the stored value is the lattice value.
    const auto finite = finite_space();
    const auto list = det::parse_candidate_list(render(finite_tokens()) + "\n", finite);
    const auto lattice_value =
        finite.candidate_at(finite.candidate_ordinal(list.at(0)), 0);
    require(list.at(0).values == lattice_value.values,
            "a finite-space vector is not its own lattice value");
}

// Signed zeros: accepted values must be the importer's, bit for bit, and must reach every
// consumer unchanged (cursor, strategy-ABI text). Nothing is normalized to match the digest.
void signed_zero_parity_and_propagation() {
    const Tokens zero_base{{"X", "0.0"}, {"Z", "0.0"}};
    const std::vector<ParityCase> zero_cases{
        {"X", "-0.0", 1}, {"X", "-0", 1}, {"X", "0", 1}, {"X", "-0e0", 1}, {"X", "1.0", 1},
        {"Z", "-0.0", 1}, {"Z", "-0", 1}, {"Z", "0", 1}, {"Z", "1.5", 1}, {"Z", "1", 0},
        {"Z", "0.5", 0}};
    check_parity("zero continuous", zero_space(), zero_base, zero_cases);
    check_parity("zero lattice", zero_lattice_space(), zero_base, zero_cases);

    const auto space = zero_space();
    const auto line = [](const char* x, const char* z) {
        return "{\"X\":" + std::string(x) + ",\"Z\":" + z + "}\n";
    };
    const auto positive = det::parse_candidate_list(line("0.0", "0.0"), space);
    const auto negative = det::parse_candidate_list(line("-0.0", "-0.0"), space);
    const auto mixed_sign = det::parse_candidate_list(line("-0.0", "0.0"), space);
    const auto real_of = [](const det::CandidateList& list, const char* name) {
        return std::get<double>(list.at(0).values.at(name));
    };
    require(!std::signbit(real_of(positive, "X")) && !std::signbit(real_of(positive, "Z")),
            "a positive zero gained a sign bit");
    require(std::signbit(real_of(negative, "X")) && std::signbit(real_of(negative, "Z")),
            "a spelled negative zero lost its sign bit on a continuous real or a real choice");
    require(std::signbit(real_of(mixed_sign, "X")) && !std::signbit(real_of(mixed_sign, "Z")),
            "signed zeros were not kept per dimension");

    // The importer's decoding is the oracle, bit for bit.
    const auto imported = importer_candidate(space, "{\"X\":-0.0,\"Z\":-0.0}");
    require(imported.has_value() && same_candidate_bits(*imported, negative.at(0)),
            "the list and the importer disagree on the bits of a spelled -0.0");
    require(!same_candidate_bits(negative.at(0), positive.at(0)),
            "the bit-exact comparison cannot tell -0.0 from +0.0");

    // One canonical identity (candidate_key ignores the sign of a zero), two source files.
    require(positive.list_sha256() == negative.list_sha256() &&
                negative.list_sha256() == mixed_sign.list_sha256(),
            "the list digest must not depend on the sign of a zero");
    require(positive.source_sha256() != negative.source_sha256() &&
                negative.source_sha256() != mixed_sign.source_sha256(),
            "the source digest must distinguish the original bytes");

    // Propagation: cursor occurrences and the strategy-ABI text carry the sign.
    det::CandidateListCursor cursor(negative);
    const auto next = cursor.next();
    require(next.has_value() && same_candidate_bits(*next, negative.at(0)),
            "the cursor changed the bits of an occurrence");
    const auto abi_negative = space.serialize_candidate(negative.at(0));
    const auto abi_positive = space.serialize_candidate(positive.at(0));
    require(abi_negative.at("X") == "-0" && abi_negative.at("Z") == "-0",
            "a negative zero did not reach the strategy-ABI text");
    require(abi_positive.at("X") == "0" && abi_positive.at("Z") == "0",
            "a positive zero reached the strategy-ABI text with a sign");
    const auto abi_imported = space.serialize_candidate(*imported);
    require(abi_imported == abi_negative,
            "the importer and the list hand the strategy different text for -0.0");

    // A finite space is canonicalized through the lattice exactly as the importer does.
    const auto lattice = zero_lattice_space();
    const auto snapped = det::parse_candidate_list(line("-0.0", "-0.0"), lattice);
    const auto lattice_imported = importer_candidate(lattice, "{\"X\":-0.0,\"Z\":-0.0}");
    require(lattice_imported.has_value() &&
                same_candidate_bits(*lattice_imported, snapped.at(0)),
            "lattice canonicalization of -0.0 differs from the importer");
    require(!std::signbit(real_of(snapped, "X")) && !std::signbit(real_of(snapped, "Z")),
            "the lattice value of zero is +0.0 for the stepped real and the declared choice");
}

void settings_and_budget() {
    using det::require_candidate_list_settings;
    const auto none = pfh::PrunerKind::None;
    const auto sampler_default = pfh::CandidatePolicy::SamplerDefault;
    // The candidates sampler with nothing else set, and other samplers untouched.
    require_candidate_list_settings(true, true, sampler_default, none, 0, false);
    require_candidate_list_settings(false, false, pfh::CandidatePolicy::Exhaustive,
                                    pfh::PrunerKind::Median, 7, true);
    const auto usage = [&](const std::string& label, bool selected, bool file,
                           pfh::CandidatePolicy policy, pfh::PrunerKind pruner,
                           std::uint64_t patience) {
        expect_refusal(label, "hpo_cli_usage", "", [&] {
            require_candidate_list_settings(selected, file, policy, pruner, patience, false);
        });
    };
    usage("sampler without file", true, false, sampler_default, none, 0);
    usage("file without sampler", false, true, sampler_default, none, 0);
    usage("without_replacement", true, true, pfh::CandidatePolicy::WithoutReplacement, none, 0);
    usage("exhaustive", true, true, pfh::CandidatePolicy::Exhaustive, none, 0);
    usage("median pruner", true, true, sampler_default, pfh::PrunerKind::Median, 0);
    usage("halving pruner", true, true, sampler_default, pfh::PrunerKind::Halving, 0);
    usage("patience 1", true, true, sampler_default, none, 1);
    usage("patience max", true, true, sampler_default, none, UINT64_MAX);
    expect_refusal("warm start", "hpo_warm_start_rejected", "", [&] {
        require_candidate_list_settings(true, true, sampler_default, none, 0, true);
    });

    // Budget: absent (0) or equal to the list length; no prefix semantics.
    det::require_candidate_list_budget(0, 12);
    det::require_candidate_list_budget(12, 12);
    expect_refusal("budget below the length", "hpo_study_spec_invalid", "sampler",
                   [&] { det::require_candidate_list_budget(11, 12); });
    expect_refusal("budget above the length", "hpo_study_spec_invalid", "sampler",
                   [&] { det::require_candidate_list_budget(13, 12); });
}

void coverage() {
    // A fresh tracker misses everything.
    det::CandidateCoverage one(1);
    require(!one.complete() && one.evaluated() == 0 && one.scored() == 0, "fresh tracker state");
    require(one.missing_ranges() == std::vector<det::CandidateCoverage::Range>{{0, 0}},
            "a fresh single-position tracker misses position 0");
    // Complete means terminal, not scored.
    one.record(0, false);
    require(one.complete() && one.evaluated() == 1 && one.scored() == 0 &&
                one.missing_ranges().empty(),
            "a terminal unscored row must complete the list");

    det::CandidateCoverage tracker(130);
    for (const std::uint64_t position : {0ULL, 1ULL, 2ULL, 5ULL, 63ULL, 64ULL, 65ULL, 129ULL})
        tracker.record(position, position % 2 == 0);
    require(tracker.evaluated() == 8 && tracker.scored() == 3, "counts after sparse records");
    require(!tracker.complete(), "a sparse tracker must not be complete");
    require(tracker.terminal(63) && tracker.terminal(64) && !tracker.terminal(62) &&
                !tracker.terminal(66) && !tracker.terminal(130),
            "terminal() wrong at word boundaries");
    const std::vector<det::CandidateCoverage::Range> expected{{3, 4}, {6, 62}, {66, 128}};
    require(tracker.missing_ranges() == expected, "missing ranges are wrong");
    for (std::uint64_t position = 0; position < 130; ++position)
        if (!tracker.terminal(position))
            tracker.record(position, false);
    require(tracker.complete() && tracker.evaluated() == 130 && tracker.scored() == 3 &&
                tracker.missing_ranges().empty(),
            "a fully recorded tracker with few scores must still be complete");

    // Contract violations are invariant failures, never silent miscounts.
    expect_refusal("position outside the list", "hpo_invariant", "",
                   [&] { tracker.record(130, true); });
    det::CandidateCoverage twice(4);
    twice.record(2, true);
    expect_refusal("position recorded twice", "hpo_invariant", "",
                   [&] { twice.record(2, true); });
    require(twice.evaluated() == 1 && twice.scored() == 1, "a refused record changed the counts");
    expect_refusal("empty tracker", "hpo_invariant", "", [&] { det::CandidateCoverage(0); });
    expect_refusal("oversized tracker", "hpo_invariant", "",
                   [&] { det::CandidateCoverage(50001); });

    // The largest tracker is bounded and handles the final partial word.
    det::CandidateCoverage large(50000);
    large.record(49999, true);
    require(large.missing_ranges() == std::vector<det::CandidateCoverage::Range>{{0, 49998}},
            "the largest tracker misses everything but its last position");
    // A prefix, a suffix and a middle gap are all reported.
    det::CandidateCoverage gaps(10);
    for (const std::uint64_t position : {3ULL, 4ULL, 5ULL})
        gaps.record(position, true);
    require(gaps.missing_ranges() == (std::vector<det::CandidateCoverage::Range>{{0, 2}, {6, 9}}),
            "prefix and suffix gaps are wrong");

    // The metadata block, one line, with the golden digests.
    const auto list = det::parse_candidate_list(golden_text, golden_space());
    det::CandidateCoverage partial(3);
    partial.record(0, true);
    partial.record(2, false);
    const std::string head = "{\"format\":\"pineforge_candidates_v1\",\"source_sha256\":\"" +
        golden_source + "\",\"list_sha256\":\"" + golden_list + "\",\"count\":3,";
    require(det::candidate_list_json(list, partial) ==
                head + "\"evaluated\":2,\"scored\":1,\"complete\":false,"
                       "\"unevaluated_ranges\":[[1,1]]}",
            "partial metadata block differs");
    det::CandidateCoverage all(3);
    all.record(1, false);
    all.record(0, false);
    all.record(2, false);
    require(det::candidate_list_json(list, all) ==
                head + "\"evaluated\":3,\"scored\":0,\"complete\":true,\"unevaluated_ranges\":[]}",
            "a complete block with zero scored rows differs");
    expect_refusal("coverage of another size", "hpo_invariant", "",
                   [&] { (void)det::candidate_list_json(list, one); });
}

}  // namespace

int main() {
    std::string pattern = (fs::temp_directory_path() / "pf_candidate_list_XXXXXX").string();
    if (::mkdtemp(pattern.data()) == nullptr) {
        std::cerr << "cannot create a scratch directory\n";
        return 1;
    }
    scratch = pattern;
    forbidden = {"PFSENTINEL", scratch.string()};
    int status = 0;
    try {
        golden_order_and_duplicates();
        canonical_spellings();
        invalid_vectors();
        json_and_utf8();
        limits();
        paths_and_file_types();
        fixed_inputs();
        importer_parity();
        signed_zero_parity_and_propagation();
        settings_and_budget();
        coverage();
        std::cout << "candidate list admission, digests, limits, file types, importer parity and "
                     "coverage passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        status = 1;
    }
    // Remove only the directory this process created.
    if (scratch.filename().string().rfind("pf_candidate_list_", 0) == 0) {
        std::error_code ignored;
        fs::remove_all(scratch, ignored);
    }
    return status;
}
