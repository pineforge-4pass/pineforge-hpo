#include "pineforge/hpo/objective.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <locale>
#include <sstream>
#include <utility>

namespace pineforge::hpo {
namespace detail {

enum class ExpressionNodeKind {
    Number,
    Metric,
    Unary,
    Binary,
    Function,
};

enum class ExpressionOperator {
    Positive,
    Negative,
    Add,
    Subtract,
    Multiply,
    Divide,
    Less,
    LessEqual,
    Greater,
    GreaterEqual,
    Equal,
    NotEqual,
};

struct ExpressionNode {
    ExpressionNodeKind kind = ExpressionNodeKind::Number;
    ExpressionOperator op = ExpressionOperator::Add;
    double number = 0.0;
    std::string text;
    std::vector<std::unique_ptr<ExpressionNode>> children;
};

}  // namespace detail

namespace {

enum class TokenKind {
    End,
    Number,
    Identifier,
    LeftParen,
    RightParen,
    Comma,
    Plus,
    Minus,
    Star,
    Slash,
    Less,
    LessEqual,
    Greater,
    GreaterEqual,
    Equal,
    NotEqual,
};

struct Token {
    Token() = default;
    Token(TokenKind token_kind,
          std::size_t token_offset,
          double token_number = 0.0,
          std::string token_text = {})
        : kind(token_kind),
          offset(token_offset),
          number(token_number),
          text(std::move(token_text)) {}

    TokenKind kind = TokenKind::End;
    std::size_t offset = 0;
    double number = 0.0;
    std::string text;
};

bool is_identifier_start(char ch) noexcept {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || ch == '_';
}

bool is_identifier_continue(char ch) noexcept {
    return is_identifier_start(ch) || (ch >= '0' && ch <= '9') || ch == '.' || ch == ':';
}

class Lexer {
public:
    explicit Lexer(const std::string& source) : source_(source) {}

    Token next() {
        while (position_ < source_.size() &&
               (source_[position_] == ' ' || source_[position_] == '\t' ||
                source_[position_] == '\r' || source_[position_] == '\n')) {
            ++position_;
        }
        if (position_ == source_.size()) {
            return {TokenKind::End, position_};
        }

        const std::size_t start = position_;
        const char ch = source_[position_];
        if ((ch >= '0' && ch <= '9') || ch == '.') {
            return lex_number();
        }
        if (is_identifier_start(ch)) {
            ++position_;
            while (position_ < source_.size() && is_identifier_continue(source_[position_])) {
                ++position_;
            }
            return {TokenKind::Identifier, start, 0.0, source_.substr(start, position_ - start)};
        }

        ++position_;
        switch (ch) {
        case '(':
            return {TokenKind::LeftParen, start};
        case ')':
            return {TokenKind::RightParen, start};
        case ',':
            return {TokenKind::Comma, start};
        case '+':
            return {TokenKind::Plus, start};
        case '-':
            return {TokenKind::Minus, start};
        case '*':
            return {TokenKind::Star, start};
        case '/':
            return {TokenKind::Slash, start};
        case '<':
            if (consume('=')) {
                return {TokenKind::LessEqual, start};
            }
            return {TokenKind::Less, start};
        case '>':
            if (consume('=')) {
                return {TokenKind::GreaterEqual, start};
            }
            return {TokenKind::Greater, start};
        case '=':
            if (consume('=')) {
                return {TokenKind::Equal, start};
            }
            throw ExpressionError("expected '=='", start);
        case '!':
            if (consume('=')) {
                return {TokenKind::NotEqual, start};
            }
            throw ExpressionError("expected '!='", start);
        default:
            throw ExpressionError(std::string("unexpected character '") + ch + "'", start);
        }
    }

private:
    bool consume(char expected) {
        if (position_ < source_.size() && source_[position_] == expected) {
            ++position_;
            return true;
        }
        return false;
    }

    Token lex_number() {
        const std::size_t start = position_;
        bool have_digits = false;
        while (position_ < source_.size() && source_[position_] >= '0' &&
               source_[position_] <= '9') {
            have_digits = true;
            ++position_;
        }
        if (position_ < source_.size() && source_[position_] == '.') {
            ++position_;
            while (position_ < source_.size() && source_[position_] >= '0' &&
                   source_[position_] <= '9') {
                have_digits = true;
                ++position_;
            }
        }
        if (!have_digits) {
            throw ExpressionError("invalid numeric literal", start);
        }
        if (position_ < source_.size() &&
            (source_[position_] == 'e' || source_[position_] == 'E')) {
            ++position_;
            if (position_ < source_.size() &&
                (source_[position_] == '+' || source_[position_] == '-')) {
                ++position_;
            }
            const std::size_t exponent_start = position_;
            while (position_ < source_.size() && source_[position_] >= '0' &&
                   source_[position_] <= '9') {
                ++position_;
            }
            if (position_ == exponent_start) {
                throw ExpressionError("invalid numeric exponent", exponent_start);
            }
        }

        const std::string text = source_.substr(start, position_ - start);
        std::istringstream input(text);
        input.imbue(std::locale::classic());
        double number = 0.0;
        input >> number;
        if (!input || !input.eof() || !std::isfinite(number)) {
            throw ExpressionError("numeric literal must be finite", start);
        }
        return {TokenKind::Number, start, number, text};
    }

    const std::string& source_;
    std::size_t position_ = 0;
};

using Node = detail::ExpressionNode;
using NodeKind = detail::ExpressionNodeKind;
using Operator = detail::ExpressionOperator;

std::unique_ptr<Node> make_unary(Operator op, std::unique_ptr<Node> child) {
    auto node = std::make_unique<Node>();
    node->kind = NodeKind::Unary;
    node->op = op;
    node->children.push_back(std::move(child));
    return node;
}

std::unique_ptr<Node> make_binary(Operator op,
                                  std::unique_ptr<Node> left,
                                  std::unique_ptr<Node> right) {
    auto node = std::make_unique<Node>();
    node->kind = NodeKind::Binary;
    node->op = op;
    node->children.push_back(std::move(left));
    node->children.push_back(std::move(right));
    return node;
}

class Parser {
public:
    explicit Parser(const std::string& source) : lexer_(source), current_(lexer_.next()) {}

    std::unique_ptr<Node> parse() {
        auto root = parse_comparison();
        if (current_.kind != TokenKind::End) {
            throw ExpressionError("unexpected token", current_.offset);
        }
        return root;
    }

    const std::vector<std::string>& identifiers() const noexcept { return identifiers_; }

private:
    void advance() { current_ = lexer_.next(); }

    std::unique_ptr<Node> parse_comparison() {
        auto left = parse_additive();
        while (true) {
            Operator op;
            switch (current_.kind) {
            case TokenKind::Less:
                op = Operator::Less;
                break;
            case TokenKind::LessEqual:
                op = Operator::LessEqual;
                break;
            case TokenKind::Greater:
                op = Operator::Greater;
                break;
            case TokenKind::GreaterEqual:
                op = Operator::GreaterEqual;
                break;
            case TokenKind::Equal:
                op = Operator::Equal;
                break;
            case TokenKind::NotEqual:
                op = Operator::NotEqual;
                break;
            default:
                return left;
            }
            advance();
            left = make_binary(op, std::move(left), parse_additive());
        }
    }

    std::unique_ptr<Node> parse_additive() {
        auto left = parse_multiplicative();
        while (current_.kind == TokenKind::Plus || current_.kind == TokenKind::Minus) {
            const Operator op =
                current_.kind == TokenKind::Plus ? Operator::Add : Operator::Subtract;
            advance();
            left = make_binary(op, std::move(left), parse_multiplicative());
        }
        return left;
    }

    std::unique_ptr<Node> parse_multiplicative() {
        auto left = parse_unary();
        while (current_.kind == TokenKind::Star || current_.kind == TokenKind::Slash) {
            const Operator op =
                current_.kind == TokenKind::Star ? Operator::Multiply : Operator::Divide;
            advance();
            left = make_binary(op, std::move(left), parse_unary());
        }
        return left;
    }

    std::unique_ptr<Node> parse_unary() {
        if (current_.kind == TokenKind::Plus) {
            advance();
            return make_unary(Operator::Positive, parse_unary());
        }
        if (current_.kind == TokenKind::Minus) {
            advance();
            return make_unary(Operator::Negative, parse_unary());
        }
        return parse_primary();
    }

    std::unique_ptr<Node> parse_primary() {
        if (current_.kind == TokenKind::Number) {
            auto node = std::make_unique<Node>();
            node->kind = NodeKind::Number;
            node->number = current_.number;
            advance();
            return node;
        }
        if (current_.kind == TokenKind::Identifier) {
            const std::string name = current_.text;
            const std::size_t offset = current_.offset;
            advance();
            if (current_.kind != TokenKind::LeftParen) {
                if (std::find(identifiers_.begin(), identifiers_.end(), name) ==
                    identifiers_.end()) {
                    identifiers_.push_back(name);
                }
                auto node = std::make_unique<Node>();
                node->kind = NodeKind::Metric;
                node->text = name;
                return node;
            }
            return parse_function(name, offset);
        }
        if (current_.kind == TokenKind::LeftParen) {
            advance();
            auto node = parse_comparison();
            if (current_.kind != TokenKind::RightParen) {
                throw ExpressionError("expected ')'", current_.offset);
            }
            advance();
            return node;
        }
        throw ExpressionError("expected a number, metric, function, or '('", current_.offset);
    }

    std::unique_ptr<Node> parse_function(const std::string& name, std::size_t offset) {
        if (name != "min" && name != "max" && name != "abs") {
            throw ExpressionError("unknown function '" + name + "'", offset);
        }
        advance();  // left parenthesis
        auto node = std::make_unique<Node>();
        node->kind = NodeKind::Function;
        node->text = name;
        if (current_.kind != TokenKind::RightParen) {
            while (true) {
                node->children.push_back(parse_comparison());
                if (current_.kind != TokenKind::Comma) {
                    break;
                }
                advance();
            }
        }
        if (current_.kind != TokenKind::RightParen) {
            throw ExpressionError("expected ')' after function arguments", current_.offset);
        }
        advance();
        const std::size_t expected = name == "abs" ? 1 : 2;
        if (node->children.size() != expected) {
            throw ExpressionError(
                "function '" + name + "' expects " + std::to_string(expected) + " argument(s)",
                offset);
        }
        return node;
    }

    Lexer lexer_;
    Token current_;
    std::vector<std::string> identifiers_;
};

ExpressionEvaluation invalid(EvaluationError error, std::string diagnostic) {
    return {false, 0.0, error, std::move(diagnostic)};
}

ExpressionEvaluation checked_result(double value, const EvaluationPolicy& policy) {
    if (!std::isfinite(value) && policy.non_finite_result == NonFinitePolicy::Reject) {
        return invalid(EvaluationError::NonFiniteResult, "expression produced a non-finite result");
    }
    return {true, value, EvaluationError::None, {}};
}

ExpressionEvaluation evaluate_node(const Node& node,
                                   const MetricMap& metrics,
                                   const EvaluationPolicy& policy) {
    if (node.kind == NodeKind::Number) {
        return {true, node.number, EvaluationError::None, {}};
    }
    if (node.kind == NodeKind::Metric) {
        const auto it = metrics.find(node.text);
        if (it == metrics.end()) {
            return invalid(EvaluationError::MissingMetric, "missing metric: " + node.text);
        }
        if (!std::isfinite(it->second) && policy.non_finite_metric == NonFinitePolicy::Reject) {
            return invalid(EvaluationError::NonFiniteMetric, "metric is non-finite: " + node.text);
        }
        return {true, it->second, EvaluationError::None, {}};
    }
    if (node.kind == NodeKind::Unary) {
        auto child = evaluate_node(*node.children[0], metrics, policy);
        if (!child.valid) {
            return child;
        }
        return checked_result(node.op == Operator::Negative ? -child.value : child.value, policy);
    }
    if (node.kind == NodeKind::Function) {
        auto first = evaluate_node(*node.children[0], metrics, policy);
        if (!first.valid) {
            return first;
        }
        if (node.text == "abs") {
            return checked_result(std::abs(first.value), policy);
        }
        auto second = evaluate_node(*node.children[1], metrics, policy);
        if (!second.valid) {
            return second;
        }
        if (std::isnan(first.value) || std::isnan(second.value)) {
            return checked_result(std::numeric_limits<double>::quiet_NaN(), policy);
        }
        const double result = node.text == "min" ? std::min(first.value, second.value)
                                                 : std::max(first.value, second.value);
        return checked_result(result, policy);
    }

    auto left = evaluate_node(*node.children[0], metrics, policy);
    if (!left.valid) {
        return left;
    }
    auto right = evaluate_node(*node.children[1], metrics, policy);
    if (!right.valid) {
        return right;
    }

    switch (node.op) {
    case Operator::Add:
        return checked_result(left.value + right.value, policy);
    case Operator::Subtract:
        return checked_result(left.value - right.value, policy);
    case Operator::Multiply:
        return checked_result(left.value * right.value, policy);
    case Operator::Divide:
        if (right.value == 0.0 && policy.division_by_zero == DivisionByZeroPolicy::Reject) {
            return invalid(EvaluationError::DivisionByZero, "division by zero");
        }
        return checked_result(left.value / right.value, policy);
    case Operator::Less:
        return {true, left.value < right.value ? 1.0 : 0.0, EvaluationError::None, {}};
    case Operator::LessEqual:
        return {true, left.value <= right.value ? 1.0 : 0.0, EvaluationError::None, {}};
    case Operator::Greater:
        return {true, left.value > right.value ? 1.0 : 0.0, EvaluationError::None, {}};
    case Operator::GreaterEqual:
        return {true, left.value >= right.value ? 1.0 : 0.0, EvaluationError::None, {}};
    case Operator::Equal:
        return {true, left.value == right.value ? 1.0 : 0.0, EvaluationError::None, {}};
    case Operator::NotEqual:
        return {true, left.value != right.value ? 1.0 : 0.0, EvaluationError::None, {}};
    case Operator::Positive:
    case Operator::Negative:
        break;
    }
    return invalid(EvaluationError::NonFiniteResult, "invalid expression operator");
}

}  // namespace

ExpressionError::ExpressionError(std::string message, std::size_t offset)
    : TypedHpoError<>("hpo_study_spec_invalid",
                      {{"reason", "objective"}},
                      std::move(message) + " at offset " + std::to_string(offset)),
      offset_(offset) {}

MetricExpression::MetricExpression(std::string source) : source_(std::move(source)) {
    if (source_.empty()) {
        throw ExpressionError("expression must not be empty", 0);
    }
    Parser parser(source_);
    root_ = parser.parse();
    identifiers_ = parser.identifiers();
}

MetricExpression::~MetricExpression() = default;
MetricExpression::MetricExpression(MetricExpression&&) noexcept = default;
MetricExpression& MetricExpression::operator=(MetricExpression&&) noexcept = default;

ExpressionEvaluation MetricExpression::evaluate(const MetricMap& metrics,
                                                const EvaluationPolicy& policy) const noexcept {
    try {
        auto result = evaluate_node(*root_, metrics, policy);
        if (result.valid && !std::isfinite(result.value) &&
            policy.non_finite_result == NonFinitePolicy::Reject) {
            return invalid(EvaluationError::NonFiniteResult,
                           "expression produced a non-finite result");
        }
        return result;
    } catch (const std::exception& error) {
        return invalid(EvaluationError::NonFiniteResult,
                       std::string("expression evaluation failed: ") + error.what());
    } catch (...) {
        return invalid(EvaluationError::NonFiniteResult, "expression evaluation failed");
    }
}

bool Constraint::satisfied() const noexcept {
    if (!std::isfinite(lhs) || !std::isfinite(rhs) || !std::isfinite(tolerance)) {
        return false;
    }
    const double effective_tolerance = std::max(0.0, tolerance);
    switch (relation) {
    case ConstraintRelation::LessEqual:
        return lhs <= rhs + effective_tolerance;
    case ConstraintRelation::GreaterEqual:
        return lhs >= rhs - effective_tolerance;
    case ConstraintRelation::Equal:
        return std::abs(lhs - rhs) <= effective_tolerance;
    }
    return false;
}

double Constraint::violation() const noexcept {
    if (!std::isfinite(lhs) || !std::isfinite(rhs) || !std::isfinite(tolerance)) {
        return std::numeric_limits<double>::infinity();
    }
    const double effective_tolerance = std::max(0.0, tolerance);
    switch (relation) {
    case ConstraintRelation::LessEqual:
        return std::max(0.0, lhs - rhs - effective_tolerance);
    case ConstraintRelation::GreaterEqual:
        return std::max(0.0, rhs - lhs - effective_tolerance);
    case ConstraintRelation::Equal:
        return std::max(0.0, std::abs(lhs - rhs) - effective_tolerance);
    }
    return std::numeric_limits<double>::infinity();
}

bool ObjectiveResult::feasible() const noexcept {
    if (!valid || values.empty()) {
        return false;
    }
    for (double value : values) {
        if (!std::isfinite(value)) {
            return false;
        }
    }
    return std::all_of(constraints.begin(), constraints.end(),
                       [](const Constraint& constraint) { return constraint.satisfied(); });
}

ObjectiveResult ObjectiveResult::invalid(std::string diagnostic) {
    ObjectiveResult result;
    result.valid = false;
    result.diagnostic = std::move(diagnostic);
    return result;
}

}  // namespace pineforge::hpo
