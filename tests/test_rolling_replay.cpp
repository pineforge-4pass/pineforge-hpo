// Replay gate for the rolling schedule.
//
// A rolling result records what each proposal saw: `completion_order` lists the trial ids in the
// order the coordinator fed them back, and `completed_before[i]` is how many of those entries had
// been fed back when trial `i` was proposed. This test runs the real pineforge-hpo-native binary
// on the rolling schedule with the TPE sampler at one, two and four workers, reads each result
// file, and re-derives every proposal from that file alone. It builds a fresh TpeSampler from the
// recorded search space, seed, sampler configuration, candidate policy, direction and trial
// budget, feeds it the recorded trials in the recorded order (a tell for a feasible trial with a
// finite objective, an abandon for any other trial), asks for the next candidate before each
// trial, and requires that candidate to equal the parameters the native run recorded for it.
//
// The test fails when the native rolling loop or its log writer is wrong: a wrong feed order
// across a prefix boundary (an order change inside one pass is invisible to every proposal of the
// default TPE configuration this test runs), a wrong completed_before value, a missing or
// repeated entry, or a log taken at the wrong moment makes some replayed proposal differ from the
// recorded one, or makes the sampler refuse the log.
// Three controls show that the replay really depends on the log and that the schedule was
// exercised:
//   (a) a log with two adjacent entries of completion_order swapped must stop agreeing;
//   (b) a log with one completed_before value changed must stop agreeing;
//   (c) the one-worker run must complete in id order with completed_before[i] == i, and at least
//       one multi-worker run must complete out of id order.
// A swap or a changed value only counts when it changes what some proposal saw, and the controls
// need at least one replayed proposal (not only a refusal of the log) to differ in the
// multi-worker runs, so a replay that ignored the log would fail them.

#include <pineforge/hpo/sampler.hpp>
#include "../src/cli/continuation.hpp"

#include <fcntl.h>
#include <spawn.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

extern char** environ;

namespace pfh = pineforge::hpo;

namespace {

using pfh::detail::Json;

int failures = 0;

void expect(bool condition, const std::string& message) {
    if (condition)
        return;
    std::cerr << "FAIL: " << message << '\n';
    ++failures;
}

constexpr std::uint64_t kBudget = 40;
constexpr std::uint64_t kSeed = 20261011;

// ---- Running the native binary -------------------------------------------------------------

// Runs the binary with its standard output discarded (the result is read from --output) and
// returns its exit status, or -1 when it could not be started or did not exit normally.
int run_native(const std::string& native, const std::vector<std::string>& arguments) {
    std::vector<std::string> storage;
    storage.push_back(native);
    storage.insert(storage.end(), arguments.begin(), arguments.end());
    std::vector<char*> argv;
    for (auto& argument : storage)
        argv.push_back(argument.data());
    argv.push_back(nullptr);

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0);
    pid_t child = 0;
    const int started =
        posix_spawn(&child, native.c_str(), &actions, nullptr, argv.data(), environ);
    posix_spawn_file_actions_destroy(&actions);
    if (started != 0) {
        std::cerr << "cannot start " << native << ": " << std::strerror(started) << '\n';
        return -1;
    }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR)
            return -1;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

void write_bars(const std::filesystem::path& csv) {
    std::ofstream out(csv, std::ios::binary | std::ios::trunc);
    out << "timestamp,open,high,low,close,volume\n";
    for (int index = 0; index < 64; ++index)
        out << (1700000000000LL + index * 60000LL) << ",100,102,99,101,10\n";
    out.flush();
    if (!out)
        throw std::runtime_error("cannot write " + csv.string());
}

// One rolling TPE study. The dimension names are in byte order so that the search space rebuilt
// from the result (which lists parameters by name) has the declaration order the run used. The
// boolean dimension named `fail` makes the test strategy fail a trial when it is true, so some
// trials are abandoned instead of told. Every other name is ignored by the test strategy, except
// that the objective grows with `Length`.
std::vector<std::string> native_arguments(const std::string& plugin,
                                          const std::filesystem::path& csv, unsigned workers,
                                          const std::filesystem::path& result) {
    return {
        "run", "--strategy", plugin, "--ohlcv", csv.string(),
        "--objective", "metrics.all.net_profit",
        "--sampler", "tpe", "--max-trials", std::to_string(kBudget),
        "--seed", std::to_string(kSeed), "--workers", std::to_string(workers),
        "--schedule", "rolling", "--tpe-startup-trials", "5",
        "--input-tf", "1", "--script-tf", "5", "--chart-timezone", "Asia/Taipei",
        "--bar-magnifier", "true", "--magnifier-samples", "6",
        "--magnifier-distribution", "triangle",
        "--fixed-input", "BatchPrefixTest", "1",
        "--real-dim", "Alpha", "0", "1", "continuous",
        "--int-dim", "Beta", "2", "40", "1",
        "--int-dim", "Length", "1", "100", "1",
        "--bool-dim", "fail",
        "--output", result.string(),
    };
}

// ---- Reading the result --------------------------------------------------------------------

struct Trial {
    std::string status;
    bool told = false;       // feasible with a finite objective: fed back as a tell
    double objective = 0.0;  // meaningful only when told
    Json parameters;
};

struct Recording {
    pfh::SearchSpace space;
    std::uint64_t seed = 0;
    std::uint64_t budget = 0;
    pfh::ObjectiveDirection direction = pfh::ObjectiveDirection::Maximize;
    pfh::CandidatePolicy policy = pfh::CandidatePolicy::SamplerDefault;
    pfh::TpeSamplerConfig config;
    std::vector<std::uint64_t> order;             // completion_order
    std::vector<std::uint64_t> completed_before;  // indexed by trial id
    std::map<std::uint64_t, Trial> trials;        // by trial id
    std::string replay_contract;
};

std::string read_file(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input)
        throw std::runtime_error("cannot read " + path.string());
    std::ostringstream content;
    content << input.rdbuf();
    return content.str();
}

std::vector<std::uint64_t> id_array(const Json& document, const std::string& name) {
    const Json& array = pfh::detail::field(document, name);
    if (array.kind != Json::Kind::Array)
        throw std::runtime_error(name + " is not an array");
    std::vector<std::uint64_t> values;
    for (const auto& item : array.items)
        values.push_back(pfh::detail::json_id(item));
    return values;
}

// Everything the replay needs comes from the result file: the space, the seed, the sampler
// configuration, the candidate policy, the direction, the budget, the log and each trial.
Recording load(const std::filesystem::path& path) {
    const Json document = pfh::detail::parse_json(read_file(path), 64u * 1024u * 1024u);
    Recording recording;
    recording.replay_contract = pfh::detail::field(document, "replay_contract").text();
    recording.space = pfh::detail::search_space_from_recorded(
        pfh::detail::field(document, "space"));
    recording.seed = pfh::detail::json_id(pfh::detail::field(document, "seed"));
    recording.budget = pfh::detail::json_id(pfh::detail::field(document, "trials_requested"));

    const std::string direction = pfh::detail::field(document, "direction").text();
    if (direction != "maximize" && direction != "minimize")
        throw std::runtime_error("unknown direction " + direction);
    recording.direction = direction == "minimize" ? pfh::ObjectiveDirection::Minimize
                                                  : pfh::ObjectiveDirection::Maximize;

    const std::string policy = pfh::detail::field(document, "candidate_policy").text();
    bool known_policy = false;
    for (const auto candidate : {pfh::CandidatePolicy::SamplerDefault,
                                 pfh::CandidatePolicy::WithoutReplacement,
                                 pfh::CandidatePolicy::Exhaustive}) {
        if (policy == pfh::candidate_policy_name(candidate)) {
            recording.policy = candidate;
            known_policy = true;
        }
    }
    if (!known_policy)
        throw std::runtime_error("unknown candidate policy " + policy);

    const Json& config = pfh::detail::field(document, "sampler_config");
    recording.config.startup_trials =
        pfh::detail::json_id(pfh::detail::field(config, "startup_trials"));
    const Json& history_switch = pfh::detail::field(config, "history_switch");
    if (history_switch.kind != Json::Kind::Null)
        recording.config.history_switch = pfh::detail::json_id(history_switch);
    recording.config.scale_ei_candidates =
        pfh::detail::json_id(pfh::detail::field(config, "scale_ei_candidates"));
    recording.config.bad_reservoir_size =
        pfh::detail::json_id(pfh::detail::field(config, "bad_reservoir_size"));
    recording.config.ei_candidates =
        pfh::detail::json_id(pfh::detail::field(config, "ei_candidates"));
    recording.config.max_threads = static_cast<std::uint32_t>(
        pfh::detail::json_id(pfh::detail::field(config, "max_threads")));
    recording.config.gamma_fraction = pfh::detail::field(config, "gamma_fraction").real();
    recording.config.gamma_cap = pfh::detail::json_id(pfh::detail::field(config, "gamma_cap"));
    recording.config.prior_weight = pfh::detail::field(config, "prior_weight").real();
    recording.config.constant_liar = pfh::detail::field(config, "constant_liar").value == "true";

    recording.order = id_array(document, "completion_order");
    recording.completed_before = id_array(document, "completed_before");

    const Json& trials = pfh::detail::field(document, "trials");
    if (trials.kind != Json::Kind::Array)
        throw std::runtime_error("trials is not an array");
    for (const auto& item : trials.items) {
        Trial trial;
        const auto id = pfh::detail::json_id(pfh::detail::field(item, "trial_id"));
        trial.status = pfh::detail::field(item, "status").text();
        const Json& objective = pfh::detail::field(item, "objective");
        const bool feasible = pfh::detail::field(item, "feasible").value == "true";
        trial.told = feasible && objective.kind == Json::Kind::Number &&
                     std::isfinite(objective.real());
        if (trial.told)
            trial.objective = objective.real();
        trial.parameters = pfh::detail::field(item, "parameters");
        recording.trials.emplace(id, std::move(trial));
    }
    return recording;
}

// ---- Replaying the log ---------------------------------------------------------------------

// True when the candidate carries exactly the recorded parameters, compared as the replayed
// value's own type (a real that happens to be whole is printed without a decimal point).
bool same_parameters(const pfh::Candidate& candidate, const Json& recorded) {
    if (recorded.kind != Json::Kind::Object || recorded.members.size() != candidate.values.size())
        return false;
    for (const auto& [name, value] : candidate.values) {
        const Json* found = recorded.find(name);
        if (!found)
            return false;
        if (const auto* integer = std::get_if<std::int64_t>(&value)) {
            if (found->kind != Json::Kind::Number ||
                found->value.find_first_of(".eE") != std::string::npos ||
                pfh::detail::json_integer(*found) != *integer)
                return false;
        } else if (const auto* real = std::get_if<double>(&value)) {
            if (found->kind != Json::Kind::Number || found->real() != *real)
                return false;
        } else if (const auto* flag = std::get_if<bool>(&value)) {
            if (found->kind != Json::Kind::Bool || (found->value == "true") != *flag)
                return false;
        } else {
            if (found->kind != Json::Kind::String ||
                found->text() != std::get<std::string>(value))
                return false;
        }
    }
    return true;
}

enum class Verdict { kAgrees, kDiffers, kRefused };

struct Outcome {
    Verdict verdict;
    std::string detail;
};

// The replay procedure. For each trial in id order: feed the entries of `order` that the trial's
// prefix adds, ask for the next candidate, and compare it with the recorded parameters. A log the
// sampler cannot follow (an id fed before it was asked, a prefix that goes backwards or past the
// end) is refused.
Outcome replay(const Recording& recording, const std::vector<std::uint64_t>& order,
               const std::vector<std::uint64_t>& completed_before) {
    try {
        pfh::TpeSampler sampler(recording.space, recording.seed, recording.direction,
                                recording.budget, recording.config, recording.policy);
        std::size_t fed = 0;
        for (std::uint64_t id = 0; id < recording.trials.size(); ++id) {
            const std::size_t prefix = static_cast<std::size_t>(
                completed_before.at(static_cast<std::size_t>(id)));
            if (prefix < fed || prefix > order.size())
                return {Verdict::kRefused,
                        "completed_before[" + std::to_string(id) + "] = " +
                            std::to_string(prefix) + " does not extend the " +
                            std::to_string(fed) + " entries already fed"};
            while (fed < prefix) {
                const std::uint64_t entry = order[fed++];
                const Trial& trial = recording.trials.at(entry);
                if (trial.told)
                    sampler.tell(entry, trial.objective);
                else
                    sampler.abandon(entry);
            }
            const auto candidate = sampler.ask();
            if (!candidate || candidate->id != id)
                return {Verdict::kDiffers,
                        "the sampler did not issue trial " + std::to_string(id)};
            if (!same_parameters(*candidate, recording.trials.at(id).parameters))
                return {Verdict::kDiffers,
                        "trial " + std::to_string(id) +
                            ": the replayed parameters differ from the recorded ones"};
        }
    } catch (const std::exception& error) {
        return {Verdict::kRefused, error.what()};
    }
    return {Verdict::kAgrees, {}};
}

struct Tally {
    std::size_t tried = 0;
    std::size_t differs = 0;  // a replayed proposal differed from the recorded one
    std::size_t refused = 0;  // the sampler or the prefix check refused the edited log

    void add(const Outcome& outcome) {
        ++tried;
        if (outcome.verdict == Verdict::kDiffers)
            ++differs;
        else if (outcome.verdict == Verdict::kRefused)
            ++refused;
    }
};

// Control (a): swap two adjacent entries of completion_order. Only a swap across a prefix
// boundary (some trial saw exactly the entries before the pair's second entry) changes what a
// proposal saw, so only those are tried.
Tally swap_control(const Recording& recording) {
    Tally tally;
    for (std::size_t position = 0; position + 1 < recording.order.size(); ++position) {
        const bool crosses = std::find(recording.completed_before.begin(),
                                       recording.completed_before.end(),
                                       static_cast<std::uint64_t>(position + 1)) !=
                             recording.completed_before.end();
        if (!crosses)
            continue;
        auto swapped = recording.order;
        std::swap(swapped[position], swapped[position + 1]);
        tally.add(replay(recording, swapped, recording.completed_before));
    }
    return tally;
}

// Control (b): change one completed_before value by one, keeping a value the log could hold (it
// never decreases and never exceeds the length of completion_order).
Tally prefix_control(const Recording& recording) {
    Tally tally;
    const auto& values = recording.completed_before;
    for (std::size_t index = 1; index < values.size(); ++index) {
        for (const int step : {-1, 1}) {
            if (step < 0 && values[index] == 0)
                continue;
            const std::uint64_t changed = step < 0 ? values[index] - 1 : values[index] + 1;
            if (changed < values[index - 1] || changed > recording.order.size())
                continue;
            if (index + 1 < values.size() && changed > values[index + 1])
                continue;
            auto edited = values;
            edited[index] = changed;
            tally.add(replay(recording, recording.order, edited));
        }
    }
    return tally;
}

// ---- The gate ------------------------------------------------------------------------------

struct Totals {
    bool out_of_order_seen = false;  // a multi-worker run completed out of id order
    std::size_t swap_differs = 0;    // multi-worker swaps that changed a replayed proposal
    std::size_t prefix_differs = 0;  // multi-worker edits that changed a replayed proposal
};

void check_run(const Recording& recording, unsigned workers, Totals& totals) {
    const std::string label = "workers=" + std::to_string(workers) + ": ";
    expect(recording.replay_contract == "rolling_completion_v1",
           label + "replay_contract is " + recording.replay_contract);
    expect(recording.budget == kBudget, label + "trials_requested is not the requested budget");
    expect(recording.trials.size() == kBudget, label + "the result does not hold every trial");
    expect(recording.order.size() == kBudget, label + "completion_order is not complete");
    expect(recording.completed_before.size() == kBudget,
           label + "completed_before has one entry per trial id");
    std::vector<std::uint64_t> id_order(kBudget);
    for (std::uint64_t id = 0; id < kBudget; ++id)
        id_order[id] = id;
    expect(std::all_of(id_order.begin(), id_order.end(),
                       [&](std::uint64_t id) { return recording.trials.count(id) == 1; }),
           label + "trial ids are not 0 to " + std::to_string(kBudget - 1));

    // Control (c).
    if (workers == 1) {
        expect(recording.order == id_order, label + "one worker must complete in id order");
        expect(recording.completed_before == id_order,
               label + "one worker: every proposal must have seen all earlier trials");
    } else if (recording.order != id_order) {
        totals.out_of_order_seen = true;
    }

    // A trial is fed back as a tell only when it is feasible with a finite objective, which only a
    // trial with status ok can be; every other trial is an abandon.
    std::size_t told = 0;
    for (const auto& entry : recording.trials) {
        told += entry.second.told ? 1 : 0;
        expect(!entry.second.told || entry.second.status == "ok",
               label + "trial " + std::to_string(entry.first) + " is feasible but has status " +
                   entry.second.status);
    }

    const Outcome primary = replay(recording, recording.order, recording.completed_before);
    expect(primary.verdict == Verdict::kAgrees,
           label + "the replay of the recorded log failed: " + primary.detail);

    const Tally swaps = swap_control(recording);
    expect(swaps.tried > 0, label + "no swap crossed a prefix boundary: control (a) not exercised");
    expect(swaps.differs + swaps.refused > 0,
           label + "swapping two completion_order entries never changed the replay");
    const Tally prefixes = prefix_control(recording);
    expect(prefixes.tried > 0, label + "no completed_before edit was possible: control (b)");
    expect(prefixes.differs + prefixes.refused > 0,
           label + "changing one completed_before value never changed the replay");
    if (workers > 1) {
        totals.swap_differs += swaps.differs;
        totals.prefix_differs += prefixes.differs;
    }
    std::cout << label << told << " told, " << (kBudget - told) << " abandoned; swaps tried "
              << swaps.tried << " (proposals differed " << swaps.differs << ", log refused "
              << swaps.refused << "); completed_before edits tried " << prefixes.tried
              << " (proposals differed " << prefixes.differs << ", log refused "
              << prefixes.refused << ")\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: " << argv[0] << " NATIVE_BINARY STRATEGY_PLUGIN\n";
        return 2;
    }
    const std::string native = argv[1];
    const std::string plugin = argv[2];
    std::filesystem::path directory;
    try {
        directory = std::filesystem::temp_directory_path() /
                    ("pineforge-rolling-replay-" + std::to_string(::getpid()));
        std::filesystem::create_directories(directory);
        const auto csv = directory / "bars.csv";
        write_bars(csv);

        Totals totals;
        for (const unsigned workers : {1u, 2u, 4u}) {
            const auto result = directory / ("result-" + std::to_string(workers) + ".json");
            const int status =
                run_native(native, native_arguments(plugin, csv, workers, result));
            expect(status == 0, "the native run with " + std::to_string(workers) +
                                    " worker(s) exited with status " + std::to_string(status));
            if (status != 0)
                continue;
            check_run(load(result), workers, totals);
        }
        // Control (c), and the controls that need a replayed proposal (not only a refusal of the
        // log) to differ: with several workers there are unfinished trials at a proposal, so an
        // edited log changes what that proposal saw.
        expect(totals.out_of_order_seen,
               "no multi-worker run completed out of id order: the schedule was not exercised");
        expect(totals.swap_differs > 0,
               "no swapped log made a multi-worker replay propose something else");
        expect(totals.prefix_differs > 0,
               "no changed completed_before made a multi-worker replay propose something else");
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        ++failures;
    }
    if (failures != 0) {
        // Keep the native results of a failing run so the logs can be read.
        std::cerr << failures << " check(s) failed; the native results are in " << directory.string()
                  << '\n';
        return 1;
    }
    std::error_code ignored;
    std::filesystem::remove_all(directory, ignored);
    std::cout << "rolling replay gate passed\n";
    return 0;
}
