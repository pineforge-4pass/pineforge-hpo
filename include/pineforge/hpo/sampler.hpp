#pragma once

#include <cstdint>
#include <cstddef>
#include <memory>
#include <optional>
#include <random>
#include <vector>

#include "pineforge/hpo/search_space.hpp"

namespace pineforge::hpo {

/// Minimal pull-based interface shared by candidate samplers.
class Sampler {
public:
    virtual ~Sampler() = default;

    /// Returns the next candidate, or `std::nullopt` when the configured budget is exhausted.
    virtual std::optional<Candidate> next() = 0;
    /// Restores the sampler's seeded initial state.
    virtual void reset() = 0;
    /// Returns the number of candidates issued since construction or the last reset.
    virtual std::uint64_t generated() const noexcept = 0;
};

/// Lazy exhaustive sampler over a finite SearchSpace in mixed-radix order.
class GridSampler final : public Sampler {
public:
    /// @throws std::invalid_argument if a varying real dimension has no step.
    explicit GridSampler(SearchSpace space);

    /// Returns each finite candidate once, then `std::nullopt`.
    std::optional<Candidate> next() override;
    /// Rewinds enumeration to ordinal zero.
    void reset() override;
    /// Returns the number of candidates issued since construction or reset.
    std::uint64_t generated() const noexcept override { return generated_; }
    /// Returns the exact finite search-space cardinality.
    std::uint64_t total_candidates() const noexcept { return total_candidates_; }

private:
    SearchSpace space_;
    std::uint64_t total_candidates_ = 0;
    std::uint64_t generated_ = 0;
};

/// @brief Seeded independent sampler with replacement.
///
/// RandomSampler is deterministic for a fixed search space, seed, and call sequence. It does not
/// learn from objective values and does not guarantee unique candidates.
class RandomSampler final : public Sampler {
public:
    /// Constructs a random sampler; `max_candidates == 0` means unbounded generation.
    RandomSampler(SearchSpace space, std::uint64_t seed, std::uint64_t max_candidates = 0);

    /// Samples one independent candidate, or returns `std::nullopt` at the configured limit.
    std::optional<Candidate> next() override;
    /// Reseeds the generator and restores the original sequence.
    void reset() override;
    /// Returns the number of candidates issued since construction or reset.
    std::uint64_t generated() const noexcept override { return generated_; }
    /// Returns the reproducibility seed.
    std::uint64_t seed() const noexcept { return seed_; }

private:
    SearchSpace space_;
    std::uint64_t seed_;
    std::uint64_t max_candidates_;
    std::uint64_t generated_ = 0;
    std::mt19937_64 engine_;
};

/// Direction used to rank a scalar objective.
enum class ObjectiveDirection {
    Maximize,  ///< Larger finite values are better.
    Minimize,  ///< Smaller finite values are better.
};

/// @brief Uniqueness and coverage contract applied to sampler proposals.
///
/// Finite policies require a positive step on every varying real dimension. Candidates are
/// reserved before ask() returns, so pending, completed, and abandoned points all remain seen.
enum class CandidatePolicy {
    SamplerDefault,      ///< Preserve native sampler behavior, including possible duplicates.
    WithoutReplacement,  ///< Issue at most the requested number of unique finite candidates.
    Exhaustive,           ///< Require a budget equal to cardinality and cover every candidate.
};

/// Returns the stable StudySpec spelling of @p policy.
const char* candidate_policy_name(CandidatePolicy policy) noexcept;

/// Derives a portable continuation RNG seed; an empty history preserves the original seed.
std::uint64_t continuation_seed(std::uint64_t seed, std::uint64_t warm_trials) noexcept;

/// One previously attempted candidate; only finite, feasible completed objectives are supplied.
struct WarmStartObservation {
    Candidate candidate;
    std::optional<double> objective;
};

/// Immutable, ordered sampler observations, optionally backed by mapped binary columns.
/// Const accessors must permit concurrent reads during independent-dimension model fitting.
class WarmStartSource {
public:
    /// Releases the source and any resources retained by its implementation.
    virtual ~WarmStartSource() = default;
    /// Returns the number of attempted trials, including abandoned trials.
    virtual std::uint64_t size() const noexcept = 0;
    /// Returns a trial ID; rows must be ordered by strictly increasing ID.
    virtual std::uint64_t id(std::uint64_t row) const = 0;
    /// Returns a parameter in search-space declaration order and original units.
    virtual ParameterValue parameter(std::uint64_t row, std::size_t dimension) const = 0;
    /// Returns a finite feasible score, or no score for an abandoned observation.
    virtual std::optional<double> objective(std::uint64_t row) const = 0;
    /// Materializes one observation without retaining other candidate maps.
    WarmStartObservation observation(const SearchSpace& space, std::uint64_t row) const;
};

/// Tuning parameters for the native product-density TPE implementation.
struct TpeSamplerConfig {
    /// Number of completed trials required before fitting Parzen estimators.
    std::uint64_t startup_trials = 10;

    /// Number of draws from `l(x)` considered for each fitted-model ask.
    ///
    /// The candidate with the largest independent-density log ratio
    /// `log(l(x)) - log(g(x))` is selected.
    std::uint64_t ei_candidates = 24;

    /// Fraction of completed observations assigned to the good estimator.
    double gamma_fraction = 0.10;
    /// Upper bound on observations assigned to the good estimator.
    ///
    /// The good set size is `ceil(gamma_fraction * completed)`, capped here and leaving at least
    /// one observation for `g(x)` whenever possible.
    std::uint64_t gamma_cap = 25;

    /// Positive smoothing mass for the numeric prior and categorical pseudocounts.
    double prior_weight = 1.0;

    /// Whether outstanding candidates are included only in the bad estimator.
    ///
    /// No objective value is fabricated and pending candidates are not counted as completed.
    /// This scale-independent constant-liar policy discourages concurrent asks from proposing
    /// the same region.
    bool constant_liar = true;

    /// Optional completed-observation threshold for bounded models; nullopt means never switch.
    std::optional<std::uint64_t> history_switch = std::nullopt;

    /// Acquisition draws after the history switch; never exceeds ei_candidates.
    std::uint64_t scale_ei_candidates = 8;

    /// Uniform older non-elite reservoir size, in addition to 64 recent observations.
    std::uint64_t bad_reservoir_size = 448;
};

/// @brief Independent, single-objective Tree-structured Parzen Estimator sampler.
///
/// Numeric dimensions use bounded Gaussian mixtures in normalized coordinates; integer and
/// stepped-real dimensions decode onto legal grids. Boolean and categorical dimensions use
/// smoothed weighted histograms. Fitted candidate generation samples from the good model `l(x)`
/// and retains the proposal maximizing `l(x) / g(x)`.
///
/// next() is an alias for ask(). Every returned candidate remains outstanding until tell() or
/// abandon() receives its ID. Public methods are thread-safe, although deterministic replay
/// requires the same ordering of ask/tell/abandon calls.
///
/// By default, proposals use the exact full-history estimator for the entire study.
/// An explicit history_switch opts into bounded models after that many completed observations.
/// Thereafter the sampler retains at most gamma_cap elite observations and 64 recent non-elite
/// observations plus bad_reservoir_size older non-elites from a separate seeded reservoir.
/// Outstanding or abandoned proposals never advance the switch. Numeric density tables,
/// elite-change good-model refits, and 32-completion bad-model epochs bound
/// work independently of study length.
class TpeSampler final : public Sampler {
public:
    /// Constructs a native TPE sampler; `max_candidates == 0` means unbounded generation.
    ///
    /// Finite policies validate exact cardinality and budget compatibility at construction.
    /// @throws std::invalid_argument for invalid configuration, direction, policy, or finite
    /// policy/budget combination.
    TpeSampler(SearchSpace space,
               std::uint64_t seed,
               ObjectiveDirection direction = ObjectiveDirection::Maximize,
               std::uint64_t max_candidates = 0,
               TpeSamplerConfig config = {},
               CandidatePolicy candidate_policy = CandidatePolicy::SamplerDefault);
    ~TpeSampler() override;

    TpeSampler(const TpeSampler&) = delete;
    TpeSampler& operator=(const TpeSampler&) = delete;
    TpeSampler(TpeSampler&&) noexcept;
    TpeSampler& operator=(TpeSampler&&) noexcept;

    /// Alias for ask().
    std::optional<Candidate> next() override;

    /// Atomically issues and reserves the next candidate.
    ///
    /// Returns `std::nullopt` at the configured budget or exact finite-space exhaustion.
    std::optional<Candidate> ask();

    /// Completes an outstanding candidate with its original finite objective value.
    /// @throws std::invalid_argument for a non-finite value or non-outstanding ID.
    void tell(std::uint64_t candidate_id, double objective_value);

    /// Removes an outstanding failed or pruned candidate without training TPE.
    /// @throws std::invalid_argument when @p candidate_id is not outstanding.
    void abandon(std::uint64_t candidate_id);

    /// Imports attempted candidates into a pristine sampler without consuming its new budget.
    /// Feasible objectives train the same estimator as tell(); other attempts only reserve points.
    /// IDs continue after the largest imported ID. No historical proposals are generated.
    /// A matching sampler_state() checkpoint restores the exact RNG/model state and returns true.
    /// Without one, history is rebuilt with continuation_seed() and returns false. The retained
    /// replay_batch_size argument is ignored; legacy histories cannot recover rejection draws.
    /// @throws std::invalid_argument for invalid candidates, IDs, or non-finite objectives.
    /// @throws std::logic_error if the sampler is not pristine.
    bool warm_start(const std::vector<WarmStartObservation>& observations,
                   std::uint64_t replay_batch_size = 0,
                   const std::string& sampler_state = {});

    /// Restores exact ordered observations while retaining the immutable source by reference.
    /// Import consumes every row; checkpoint restoration matches the vector overload.
    bool warm_start(std::shared_ptr<const WarmStartSource> source,
                    std::uint64_t replay_batch_size = 0,
                    const std::string& sampler_state = {});

    /// Returns a versioned, checksummed checkpoint bound to the configuration and observations.
    /// No candidate may be outstanding. Preserve this alongside the complete attempted history.
    /// Numeric caches retain their fitting observation IDs, not redundant kernel arrays.
    /// @throws std::logic_error if a candidate is outstanding.
    std::string sampler_state() const;

    /// Restores the seeded initial state and clears finite-space reservations.
    ///
    /// @throws std::logic_error while a candidate is outstanding, preventing a late pre-reset
    /// result from being accepted after candidate IDs are reused.
    void reset() override;
    /// Returns the number of candidates issued since construction or reset.
    std::uint64_t generated() const noexcept override;
    /// Returns the number of candidates completed through tell().
    std::uint64_t completed() const noexcept;
    /// Returns the number of candidates awaiting tell() or abandon().
    std::uint64_t outstanding() const noexcept;
    /// Returns the bounded number of completed observations kept for density estimation.
    std::size_t retained_observations() const;
    /// Returns internal duplicate proposals rejected by a finite policy.
    std::uint64_t duplicate_proposals_skipped() const noexcept;
    /// Returns the reproducibility seed.
    std::uint64_t seed() const noexcept { return seed_; }
    /// Returns the configured optimization direction.
    ObjectiveDirection direction() const noexcept { return direction_; }
    /// Returns the immutable TPE tuning configuration.
    const TpeSamplerConfig& config() const noexcept { return config_; }
    /// Returns the configured uniqueness/coverage policy.
    CandidatePolicy candidate_policy() const noexcept { return candidate_policy_; }

private:
    class Impl;

    SearchSpace space_;
    std::uint64_t seed_;
    ObjectiveDirection direction_;
    std::uint64_t max_candidates_;
    TpeSamplerConfig config_;
    CandidatePolicy candidate_policy_;
    std::unique_ptr<Impl> impl_;
};

/// @brief Adaptive derivative-free optimizer backed by `dlib::global_function_search`.
///
/// next() is an alias for ask(). Every returned candidate remains outstanding until tell() or
/// abandon() receives its ID. Multiple candidates may be outstanding, allowing external
/// concurrent evaluation while retaining dlib's native ask/tell behavior. Public operations are
/// serialized internally.
class DlibGlobalSampler final : public Sampler {
public:
    /// Constructs a dlib global sampler; `max_candidates == 0` means unbounded generation.
    ///
    /// A fixed seed and ask/tell ordering are deterministic.
    /// @throws std::invalid_argument when the seed exceeds dlib's signed 32-bit range or the
    /// search space cannot be represented by the adapter.
    DlibGlobalSampler(SearchSpace space,
                      std::uint64_t seed,
                      ObjectiveDirection direction = ObjectiveDirection::Maximize,
                      std::uint64_t max_candidates = 0);
    ~DlibGlobalSampler() override;

    DlibGlobalSampler(const DlibGlobalSampler&) = delete;
    DlibGlobalSampler& operator=(const DlibGlobalSampler&) = delete;
    DlibGlobalSampler(DlibGlobalSampler&&) noexcept;
    DlibGlobalSampler& operator=(DlibGlobalSampler&&) noexcept;

    /// Alias for ask().
    std::optional<Candidate> next() override;

    /// Issues the next dlib evaluation request, or `std::nullopt` at the budget.
    std::optional<Candidate> ask();

    /// Completes an outstanding request with its original finite objective value.
    ///
    /// Minimize mode negates the value before passing it to dlib, whose optimizer maximizes.
    /// @throws std::invalid_argument for a non-finite value or non-outstanding ID.
    void tell(std::uint64_t candidate_id, double objective_value);

    /// Cancels an outstanding failed or pruned trial without fabricating a score.
    /// @throws std::invalid_argument when @p candidate_id is not outstanding.
    void abandon(std::uint64_t candidate_id);

    /// Cancels all outstanding requests and restores the seeded initial state.
    void reset() override;
    /// Returns the number of candidates issued since construction or reset.
    std::uint64_t generated() const noexcept override;
    /// Returns the number of candidates completed through tell().
    std::uint64_t completed() const noexcept;
    /// Returns the number of candidates awaiting tell() or abandon().
    std::uint64_t outstanding() const noexcept;
    /// Returns the reproducibility seed.
    std::uint64_t seed() const noexcept { return seed_; }
    /// Returns the configured optimization direction.
    ObjectiveDirection direction() const noexcept { return direction_; }

private:
    class Impl;

    SearchSpace space_;
    std::uint64_t seed_;
    ObjectiveDirection direction_;
    std::uint64_t max_candidates_;
    std::unique_ptr<Impl> impl_;
};

}  // namespace pineforge::hpo
