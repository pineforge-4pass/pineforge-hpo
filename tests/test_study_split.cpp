// Compile the real CLI in this test executable, with only its entry point renamed. Private
// child modes below exercise the in-process seam; the shipping CLI gets no test flag or hook.
#define main pineforge_hpo_native_entry
#include "../src/cli/main.cpp"
#undef main

#include <sys/resource.h>
#include <sys/wait.h>

#include <iterator>

namespace {

namespace cli = pineforge::hpo::cli;
namespace detail = pineforge::hpo::detail;
using Path = std::filesystem::path;

static_assert(std::is_same_v<decltype(cli::StudyResult::best_candidate),
                             std::optional<pfh::Candidate>>,
              "StudyResult must own Candidate.values, not borrow an archive record");

void require(bool condition, const std::string& message) {
    if (!condition)
        throw std::runtime_error(message);
}

void refuse_name(const Path& path) {
    std::vector<std::string> parts;
    for (const auto& part : path) {
        const auto name = part.string();
        require(name != ".ssh" && name != ".aws" && name != ".codex" &&
                    !(name.size() >= 4 && name.compare(name.size() - 4, 4, ".env") == 0),
                "refused protected path by name: " + path.string());
        parts.push_back(name);
    }
    for (std::size_t index = 0; index < parts.size(); ++index) {
        if (parts[index] != ".config")
            continue;
        require(index + 1 == parts.size() || parts[index + 1] != "pineforge",
                "refused protected path by name: " + path.string());
        require(index + 2 >= parts.size() || parts[index + 1] != "zsh" ||
                    parts[index + 2] != "keys",
                "refused protected path by name: " + path.string());
    }
}

Path checked(const Path& path) {
    refuse_name(path);
    const auto absolute = std::filesystem::absolute(path).lexically_normal();
    refuse_name(absolute);
    const auto resolved = std::filesystem::weakly_canonical(absolute);
    refuse_name(resolved);
    return resolved;
}

void files(const std::string& label, const std::vector<Path>& paths) {
    std::cerr << "FILES " << label << ":\n";
    for (const auto& path : paths)
        std::cerr << "  " << checked(path) << '\n';
    std::cerr.flush();
}

Path spool_directory() {
    const char* directory = std::getenv("TMPDIR");
    require(directory && *directory, "private test entry requires an explicit TMPDIR");
    return checked(directory);
}

void guard_native_arguments(int argc, char** argv) {
    std::vector<Path> followed;
    const std::vector<std::string> path_flags{
        "--strategy", "--ohlcv", "--ohlcv-bin", "--output", "--trials-file", "--warm-start",
        "--candidates", "--syminfo", "--symbol-feeds", "--symbol-feeds-spec",
        "--scheduler-stats", "--proof-curves"};
    for (int index = 1; index + 1 < argc; ++index) {
        const std::string flag = argv[index];
        require(flag != "--symbol-feeds" && flag != "--symbol-feeds-spec" &&
                    flag != "--warm-start",
                "private fixture entry refuses indirect file inventories: " + flag);
        if (std::find(path_flags.begin(), path_flags.end(), flag) == path_flags.end())
            continue;
        const auto path = checked(argv[++index]);
        followed.push_back(path);
        if (flag == "--strategy") {
            followed.push_back(checked(path.parent_path() / "manifest.json"));
            followed.push_back(checked(path.parent_path() / "provenance.json"));
        } else if (flag == "--ohlcv-bin") {
            followed.push_back(checked(path.string() + ".json"));
        }
    }
    followed.push_back(spool_directory());
    // run_child already printed this inventory before exec. A standalone private invocation
    // prints it here as well; protection checks above are unconditional in both cases.
    if (!std::getenv("PFH_STUDY_FILES_LISTED"))
        files("private native entry", followed);
}

void write_file(const Path& path, const std::string& text) {
    files("write", {path});
    std::ofstream output(checked(path), std::ios::binary | std::ios::trunc);
    output << text;
    output.close();
    require(static_cast<bool>(output), "cannot write " + path.string());
}

std::string read_file(const Path& path) {
    files("read", {path});
    std::ifstream input(checked(path), std::ios::binary);
    require(static_cast<bool>(input), "cannot open " + path.string());
    std::string text{std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
    require(!input.bad(), "cannot read " + path.string());
    return text;
}

std::string quoted(const std::string& text) {
    return detail::dump_json(detail::Json::string(text));
}

struct ChildRow {
    std::string label;
    Path output;
    Path errors;
    Path exit_file;
    Path result;
    int code = -1;
    std::int64_t peak_bytes = 0;
};

void row_verdict(const ChildRow& row, const std::string& verdict, const std::string& reason) {
    write_file(row.result, "{\"verdict\":" + quoted(verdict) + ",\"reason\":" + quoted(reason) +
        ",\"exit_code\":" + std::to_string(row.code) +
        ",\"peak_rss_bytes\":" + std::to_string(row.peak_bytes) +
        ",\"stdout\":" + quoted(row.output.string()) +
        ",\"stderr\":" + quoted(row.errors.string()) + "}\n");
}

ChildRow run_child(const Path& root, const std::string& label,
                   const std::vector<std::string>& command, std::vector<Path> followed) {
    ChildRow row{label, root / (label + ".stdout"), root / (label + ".stderr"),
                 root / (label + ".exit"), root / (label + ".result.json")};
    followed.insert(followed.end(), {command.at(0), row.output, row.errors, row.exit_file,
                                    row.result, root});
    for (std::size_t index = 1; index < command.size(); ++index) {
        if (command[index] == "--strategy" && index + 1 < command.size()) {
            const auto parent = checked(command[index + 1]).parent_path();
            followed.push_back(parent / "manifest.json");
            followed.push_back(parent / "provenance.json");
        }
    }
    files(label + " (including the anonymous spool directory)", followed);
    std::cerr << "COMMAND";
    for (const auto& argument : command)
        std::cerr << ' ' << quoted(argument);
    std::cerr << '\n';
    std::cerr.flush();
    std::cout.flush();
    std::vector<char*> arguments;
    for (const auto& argument : command)
        arguments.push_back(const_cast<char*>(argument.c_str()));
    arguments.push_back(nullptr);
    const auto pid = ::fork();
    require(pid >= 0, "fork failed");
    if (pid == 0) {
        const int output = ::open(row.output.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        const int errors = ::open(row.errors.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (output < 0 || errors < 0 || ::dup2(output, STDOUT_FILENO) < 0 ||
            ::dup2(errors, STDERR_FILENO) < 0 || ::setenv("TMPDIR", root.c_str(), 1) != 0 ||
            ::setenv("PFH_STUDY_FILES_LISTED", "1", 1) != 0)
            ::_exit(126);
        ::close(output);
        ::close(errors);
        ::execv(arguments[0], arguments.data());
        ::_exit(127);
    }
    int status = 0;
    struct rusage usage {};
    pid_t waited;
    do {
        waited = ::wait4(pid, &status, 0, &usage);
    } while (waited < 0 && errno == EINTR);
    require(waited == pid, "wait4 did not reap the work process");
    row.code = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
#if defined(__APPLE__)
    row.peak_bytes = usage.ru_maxrss;
#else
    row.peak_bytes = static_cast<std::int64_t>(usage.ru_maxrss) * 1024;
#endif
    // This is the work's status, including a negative signal number, never a wrapper status.
    write_file(row.exit_file, std::to_string(row.code) + "\n");
    row_verdict(row, "FAIL", "work output has not passed its assertions");
    return row;
}

std::string fixture_csv() {
    std::ostringstream output;
    output << "timestamp,open,high,low,close,volume\n";
    for (std::int64_t value = 1; value <= 9; ++value)
        output << value * 1'000'000'000'000LL << ',' << value << ',' << value + 2 << ','
               << value - 1 << ',' << value + 1 << ',' << value + 3 << '\n';
    return output.str();
}

// Closed-form integer oracle for the fixture's consecutive v values. This reads no report and
// calls no production evaluator, renderer or metric code. Each bar contributes 16*v+20.
int fixture_profit(int first, int last, int length) {
    const int count = last - first + 1;
    const int sum = (first + last) * count / 2;
    return 100'000 / 1'000 + length + 16 * sum + 20 * count;
}

std::vector<std::string> ordinary_command(const Path& native, const Path& plugin,
                                         const Path& csv, const Path& output) {
    return {native.string(), "run", "--strategy", plugin.string(), "--ohlcv", csv.string(),
        "--output", output.string(), "--objective", "metrics.all.net_profit",
        "--sampler", "grid", "--seed", "17", "--workers", "1", "--batch-size", "1",
        "--max-trials", "9", "--input-tf", "1", "--script-tf", "5",
        "--bar-magnifier", "true", "--magnifier-samples", "6",
        "--magnifier-distribution", "triangle", "--int-dim", "Length", "1", "9", "1",
        "--fixed-input", "BatchPrefixTest", "1", "--fixed-input", "BatchPrefixJitter", "0",
        "--fixed-input", "ChartBarsTest", "1"};
}

// Independent wire pin, not rendered by production code and never recorded from a runner.
// For v=1..9 the fixture contributes 16*v+20 per bar: sum=900. With capital=100000,
// candidate Length=L scores 1000+L; IDs are L-1. Every number below is integer-exact.
std::string ordinary_expected() {
    // SHA-256 independently computed with Python hashlib; AR-CX recomputed it on 2026-10-25
    // and matched the literal below. Canonical preimage bytes (no trailing newline):
    // {"objective":{"constraints":[],"direction":"maximize","expression":"metrics.all.net_profit"},"parameters":{"Length":{"high":"9","kind":"integer","log":false,"low":"1","step":"1"}},"space_hash_version":1}
    const std::string hash = "1eec48bb94bd90a78c98a39c757b9b1fc6a9c73198e6159ed1259f5cb4d30868";
    const std::string space =
        R"({"objective":{"constraints":[],"direction":"maximize",)"
        R"("expression":"metrics.all.net_profit"},"parameters":{"Length":)"
        R"({"high":9,"kind":"integer","log":false,"low":1,"step":1}}})";
    std::ostringstream output;
    output << "{\n"
        << "  \"schema_version\": 1,\n"
        << "  \"space_hash_version\": 1,\n"
        << "  \"space_hash\": \"" << hash << "\",\n"
        << "  \"space\": " << space << ",\n"
        << "  \"pineforge_hpo_version\": \"" << PINEFORGE_HPO_VERSION << "\",\n"
        << "  \"sampler_implementation\": \"pineforge_grid_v2_finite\",\n"
        << "  \"ok\": true,\n"
        << "  \"artifact_key\": \"\",\n"
        << "  \"sampler\": \"grid\",\n"
        << "  \"candidate_policy\": \"sampler_default\",\n"
        << "  \"candidate_policy_implementation\": \"sampler_default\",\n"
        << "  \"seed\": 17,\n"
        << "  \"tpe_history_switch\": null,\n"
        << "  \"sampler_config\": {},\n"
        << "  \"direction\": \"maximize\",\n"
        << "  \"batch_size\": 1,\n"
        << "  \"batch_lag\": 0,\n"
        << "  \"replay_contract\": \"ordered_batches_v1\",\n"
        << "  \"continuation_contract\": \"ordered_batches_v1\",\n"
        << "  \"pruner\": \"none\",\n"
        << "  \"pruner_eta\": 2,\n"
        << "  \"pruner_rungs\": [0.25, 0.5],\n"
        << "  \"search_space_finite\": true,\n"
        << "  \"search_space_cardinality_overflow\": false,\n"
        << "  \"search_space_cardinality\": 9,\n"
        << "  \"trials_requested\": 9,\n"
        << "  \"trials_completed\": 9,\n"
        << "  \"unique_candidates_attempted\": 9,\n"
        << "  \"duplicate_proposals_skipped\": 0,\n"
        << "  \"remaining_candidates\": 0,\n"
        << "  \"search_space_exhausted\": true,\n"
        << "  \"stop_reason\": \"search_space_exhausted\",\n"
        << "  \"full_parameter_coverage\": true,\n"
        << "  \"exhaustive_equivalent\": true,\n"
        << "  \"best_trial_id\": " << 9 - 1 << ",\n"
        << "  \"best_value\": " << fixture_profit(1, 9, 9) << ",\n"
        << "  \"trials\": [\n";
    for (int length = 1; length <= 9; ++length) {
        const int score = fixture_profit(1, 9, length);
        output << "    {\"trial_id\": " << length - 1
            << ", \"status\": \"ok\", \"feasible\": true, \"objective\": " << score
            << ", \"total_trades\": " << length << ", \"net_profit\": " << score
            << ", \"backtest\": {\"input_bars_processed\": 9, \"script_bars_processed\": 1, "
            << "\"input_tf_seconds\": 60, \"script_tf_seconds\": 300, \"script_tf_ratio\": 5, "
            << "\"needs_aggregation\": true, \"magnifier_sample_ticks_total\": 72}, "
            << "\"parameters\": {\"Length\": " << length
            << "}, \"constraint_values\": [], \"metrics\": {\"metrics.all.net_profit\": " << score
            << "}, \"error\": \"\", \"failure_code\": null, \"failure_args\": null, "
            << "\"failure_origin\": null, \"space_hash_version\": 1, \"space_hash\": \""
            << hash << "\", \"space\": " << space << "}" << (length == 9 ? "\n" : ",\n");
    }
    output << "  ],\n  \"trials_out\":\"all\",\n  \"best_k\":10\n}\n";
    return output.str();
}

cli::Options study_options(const Path& plugin, const Path& csv) {
    cli::Options options;
    options.strategy = plugin;
    options.ohlcv = csv;
    options.objective = "metrics.all.net_profit";
    options.seed = 17;
    options.max_trials = 9;
    options.batch_size = 1;
    options.input_timeframe = "1";
    options.script_timeframe = "5";
    options.bar_magnifier = true;
    options.magnifier_samples = 6;
    options.magnifier_distribution = PF_MAGNIFIER_TRIANGLE;
    options.dimensions.emplace_back(pfh::IntegerDimension("Length", 1, 9, 1));
    options.fixed_inputs = {{"BatchPrefixTest", "1"}, {"BatchPrefixJitter", "0"},
                            {"ChartBarsTest", "1"}};
    return options;
}

void child_view(const Path& plugin, const Path& csv, bool blocks) {
    files("view study inputs", {plugin, plugin.parent_path() / "manifest.json",
                                plugin.parent_path() / "provenance.json", csv, spool_directory()});
    auto options = study_options(plugin, csv);
    options.pruner = pfh::PrunerKind::Median;
    options.pruner_name = "median";
    options.pruner_rungs = {0.5};
    if (blocks) {
        options.pbo_blocks = 2;
        options.fixed_inputs.emplace("CurveEquity", "1,2,1.5,1.5,1.875");
        options.fixed_inputs.emplace("PrefixCurveEquity", "1,2");
        options.fixed_inputs.emplace("FullWindowBars", "4");
    }
    auto prepared = cli::prepare(std::move(options));
    const pfh::DatasetView view(prepared.dataset, 2, 4);
    auto copied = std::make_shared<const pfh::Dataset>(
        std::vector<pf_bar_t>(view.data(), view.data() + view.size()));
    std::string bytes;
    cli::StudyCallbacks callbacks;
    callbacks.publish = [&](const detail::ResultDocument& document, const cli::StudyStatistics&) {
        std::ostringstream output;
        document.write(output);
        bytes = output.str();
    };
    const auto sliced_result = cli::run_study(prepared, view, callbacks);
    const auto sliced_bytes = bytes;
    const auto copied_result = cli::run_study(prepared, pfh::DatasetView(copied), callbacks);
    require(sliced_bytes == bytes, "view and copied-subset study documents differ");
    require(sliced_result.exit_code == 0 && copied_result.exit_code == 0,
            "view/copy study did not select a winner");
    require(sliced_result.best_candidate && copied_result.best_candidate,
            "study did not return an owned candidate");
    require(sliced_result.best_candidate->values == copied_result.best_candidate->values &&
                std::get<std::int64_t>(sliced_result.best_candidate->values.at("Length")) == 9 &&
                sliced_result.best_value == fixture_profit(3, 6, 9),
            "view winner disagrees with independent fixture arithmetic");
    const auto document = detail::parse_json(bytes);
    const auto& trials = detail::field(document, "trials").items;
    require(trials.size() == 9, "view study lost terminal rows");
    for (std::size_t index = 0; index < trials.size(); ++index) {
        const auto& trial = trials[index];
        require(detail::field(trial, "status").text() == "ok", "view full rung was not scored");
        const int length = static_cast<int>(index) + 1;
        require(detail::field(trial, "objective").real() == fixture_profit(3, 6, length),
                "view trial score disagrees with arithmetic");
        const auto& pruning = detail::field(trial, "pruning");
        require(detail::json_integer(detail::field(pruning, "bars_processed_total")) == 6,
                "pruner did not use view lengths 2 and 4");
        const auto& scores = detail::field(pruning, "rung_scores").items;
        require(scores.size() == 1 && scores[0].real() == fixture_profit(3, 4, length),
                "view prefix used the wrong bars");
        if (blocks) {
            const auto& moments = detail::field(trial, "block_returns");
            require(detail::field(moments, "status").text() == "ok", "view blocks are absent");
            const auto& sums = detail::field(moments, "sum").items;
            const auto& m2 = detail::field(moments, "m2").items;
            require(sums.size() == 2 && sums[0].real() == 0.75 && sums[1].real() == 0.25 &&
                        m2.size() == 2 && m2[0].real() == 0.78125 && m2[1].real() == 0.03125,
                    "view study blocks disagree with independent dyadic arithmetic");
        }
    }
    // Returned candidate values survive the first archive/spool and a second study.
    const auto saved = sliced_result.best_candidate->values;
    require(saved == copied_result.best_candidate->values, "returned candidate lost its values");
    std::cout << "{\"verdict\":\"PASS\",\"trials\":" << trials.size()
              << ",\"begin\":" << view.begin << ",\"length\":" << view.size()
              << ",\"prefix_bars\":2,\"best_value\":" << *sliced_result.best_value << ",\"blocks\":"
              << (blocks ? "true" : "false") << "}\n";
}

std::size_t fd_count(const Path& directory) {
    std::size_t count = 0;
    for (const auto& entry : std::filesystem::directory_iterator(directory)) {
        (void)entry;
        ++count;
    }
    return count;
}

class CountingBuffer final : public std::streambuf {
public:
    std::uint64_t bytes = 0;

private:
    std::streamsize xsputn(const char*, std::streamsize count) override {
        bytes += static_cast<std::uint64_t>(count);
        return count;
    }
    int_type overflow(int_type value) override {
        if (!traits_type::eq_int_type(value, traits_type::eof()))
            ++bytes;
        return traits_type::not_eof(value);
    }
};

void child_lifecycle(const Path& plugin, const Path& csv, int studies, const std::string& control) {
#if defined(__linux__)
    const Path descriptors = "/proc/self/fd";
#else
    const Path descriptors = "/dev/fd";
#endif
    const auto trials_file = csv.parent_path() /
        ("lifecycle-" + control + "-" + std::to_string(studies) + ".trials.jsonl");
    files("sequential study inputs and fd inventory",
          {plugin, plugin.parent_path() / "manifest.json",
           plugin.parent_path() / "provenance.json", csv, descriptors, trials_file,
           spool_directory()});
    auto options = study_options(plugin, csv);
    options.sampler = "random";
    options.max_trials = 1000;
    options.trials_file = trials_file;
    options.fixed_inputs.erase("ChartBarsTest");
    options.dimensions.clear();
    options.dimensions.emplace_back(pfh::RealDimension("Length", 1.0, 100.0));
    const auto prepared = cli::prepare(std::move(options));
    CountingBuffer buffer;
    std::ostream discarded(&buffer);
    cli::StudyCallbacks callbacks;
    callbacks.publish = [&](const detail::ResultDocument& document, const cli::StudyStatistics&) {
        document.write(discarded);  // Exercise sealing AND reading the real anonymous spool.
    };
    const auto one_study = [&] {
        const auto before = buffer.bytes;
        const auto result = cli::run_study(prepared, pfh::DatasetView(prepared.dataset), callbacks);
        require(result.exit_code == 0 && result.trials_completed == 1000 && result.best_candidate &&
                    result.best_value >= 101 && result.best_value <= 200,
                "lifecycle iteration did not execute a complete real study");
        require(buffer.bytes - before > 1000 * 128,
                "lifecycle iteration did not read its completed trial spool");
    };
    for (int warmup = 0; warmup < 4; ++warmup)
        one_study();
    const auto baseline = fd_count(descriptors);
    {
        auto stopped_callbacks = callbacks;
        stopped_callbacks.cancelled = [] { return true; };
        const auto stopped = cli::run_study(
            prepared, pfh::DatasetView(prepared.dataset), stopped_callbacks);
        require(stopped.exit_code == 2 && stopped.trials_completed == 0 &&
                    !stopped.best_candidate,
                "cancelled study unexpectedly selected a candidate");
    }
    require(fd_count(descriptors) == baseline, "cancelled study retained descriptors");
    std::vector<std::size_t> counts;
    std::vector<std::unique_ptr<char[]>> retained;
    for (int index = 0; index < studies; ++index) {
        one_study();
        if (control == "fd") {
            require(::dup(STDOUT_FILENO) >= 0, "cannot plant fd retention");
        } else if (control == "rss") {
            auto pages = std::make_unique<char[]>(1024 * 1024);
            // Touch every page; reserved address space is not RSS evidence.
            volatile char* const touched = pages.get();
            for (std::size_t offset = 0; offset < 1024 * 1024; offset += 4096)
                touched[offset] = static_cast<char>(index);
            retained.push_back(std::move(pages));
        }
        counts.push_back(fd_count(descriptors));
    }
    std::cout << "{\"verdict\":\"PASS\",\"studies\":" << studies
              << ",\"trials_per_study\":1000,\"spool_bytes_read\":" << buffer.bytes
              << ",\"baseline_fds\":" << baseline << ",\"fds\":[";
    for (std::size_t index = 0; index < counts.size(); ++index)
        std::cout << (index ? "," : "") << counts[index];
    std::cout << "]}\n";
}

void check_lifecycle_document(const detail::Json& document, int studies) {
    require(detail::field(document, "verdict").text() == "PASS" &&
                detail::json_integer(detail::field(document, "studies")) == studies &&
                detail::json_integer(detail::field(document, "trials_per_study")) == 1000 &&
                detail::field(document, "spool_bytes_read").real() > 1000 * studies,
            "lifecycle child did not prove completed studies and spool reads");
    const auto baseline = detail::json_integer(detail::field(document, "baseline_fds"));
    require(baseline > 0, "missing lifecycle descriptor baseline");
    const auto& counts = detail::field(document, "fds").items;
    require(counts.size() == static_cast<std::size_t>(studies), "missing lifecycle fd samples");
}

void check_fds(const detail::Json& document, int studies) {
    check_lifecycle_document(document, studies);
    const auto baseline = detail::json_integer(detail::field(document, "baseline_fds"));
    const auto& counts = detail::field(document, "fds").items;
    for (const auto& count : counts)
        require(detail::json_integer(count) == baseline,
                "sequential study retained file descriptors");
}

#ifdef PINEFORGE_HPO_STUDY_RSS_GATE
void check_rss(const ChildRow& one, const ChildRow& many) {
    require(one.peak_bytes > 0 && many.peak_bytes > 0, "missing wait4 peak RSS");
    // Same strict 32 bytes/trial bound as the high-count CI retention gate. There are 49,000
    // additional trials across 49 additional studies, allowing 1,568,000 bytes of peak variation.
    require(many.peak_bytes - one.peak_bytes <= 32LL * 49 * 1000,
            "sequential study RSS slope exceeds the existing 32 bytes/trial gate");
}
#endif

template <typename Function>
void must_reject(Function check, const std::string& message) {
    try {
        check();
    } catch (const std::runtime_error&) {
        return;
    }
    throw std::runtime_error(message);
}

void suite(const Path& self, const Path& native, const Path& plugin, const Path& root) {
    const auto csv = root / "bars.csv";
    const auto manifest = plugin.parent_path() / "manifest.json";
    files("suite inputs", {self, native, plugin, manifest,
                           plugin.parent_path() / "provenance.json", root});
    write_file(csv, fixture_csv());

    const auto crash = run_child(root, "deliberate-crash", {self.string(), "child-crash"}, {});
    require(crash.code == -SIGKILL && read_file(crash.exit_file) == std::to_string(-SIGKILL) + "\n",
            "crash row did not preserve the work's nonzero signal exit");
    row_verdict(crash, "PASS", "deliberate SIGKILL recorded as the work's own negative exit");

    // (a) Proves all stdout/result bytes, empty stderr and exit 0. False pass: comparing parent
    // and head only, using production renderers for expected bytes, or checking best_value only.
    const auto output = root / "ordinary.json";
    const auto pin = run_child(root, "ordinary", ordinary_command(native, plugin, csv, output),
                               {plugin, manifest, csv, output});
    const auto expected = ordinary_expected();
    write_file(root / "ordinary.expected.json", expected);
    require(pin.code == 0 && read_file(pin.errors).empty(), "ordinary study changed exit/stderr");
    require(read_file(pin.output) == expected && read_file(output) == expected,
            "ordinary stdout/result file differ from the hand-derived wire pin");
    row_verdict(pin, "PASS", "all stdout/result bytes equal the independent nine-trial pin");

    // (b) Proves studies, not just executor calls, respect the offset, full-rung classification
    // and view-based pruner lengths. False pass: comparing two wrong subsets without arithmetic.
    const auto view = run_child(root, "view-study",
        {self.string(), "child-view", plugin.string(), csv.string()}, {plugin, manifest, csv});
    require(view.code == 0 &&
                detail::field(detail::parse_json(read_file(view.output)), "verdict").text() ==
                    "PASS",
            "view-study work result failed: " + read_file(view.errors));
    row_verdict(view, "PASS", "view and copied-subset studies agree with independent arithmetic");
    if (pfh::return_stats_identity_bound()) {
        const auto blocks = run_child(root, "view-blocks",
            {self.string(), "child-view-blocks", plugin.string(), csv.string()},
            {plugin, manifest, csv});
        require(blocks.code == 0 &&
                    detail::field(detail::parse_json(read_file(blocks.output)), "blocks").value ==
                        "true",
                "view full-window block study failed: " + read_file(blocks.errors));
        row_verdict(blocks, "PASS",
                    "full-view blocks match dyadic arithmetic; prefixes stay separate");
    } else {
        const char* required = std::getenv("PFH_REQUIRE_RETURN_STATS_IDENTITY");
        require(!required || std::string(required) != "1",
                "required statistics identity is unbound");
        write_file(root / "view-blocks.result.json",
            "{\"verdict\":\"UNAVAILABLE\",\"reason\":\"statistics identity is unbound\"}\n");
    }

    // (c) The real proof writer throws AFTER feasibility and score are set for Length=2:
    // its SequenceCurveEquity entry "," parses to an empty curve. The ties case has scores
    // (-1,0,-1) or (1,0,1): old selection chooses error id=1, correct selection chooses id=0.
    // The constraint case makes Length=3 score best but infeasible, with error id=1 next.
    // False pass: clearing feasibility, dropping error rows, or testing a synthetic TrialRecord.
    for (const std::string direction : {"maximize", "minimize"}) {
        for (const bool constrained : {false, true}) {
            for (const std::string retention : {"all", "best-k", "none"}) {
                const auto label = "selection-" + direction +
                    (constrained ? "-constraint-" : "-ties-") + retention;
                const auto result = root / (label + ".json");
                const auto stream = root / (label + ".trials.jsonl");
                const auto proof = root / (label + ".proof.jsonl");
                const bool maximize = direction == "maximize";
                const auto objective = constrained
                    ? (maximize ? "metrics.all.num_trades" : "-metrics.all.num_trades")
                    : (maximize ? "-abs(metrics.all.num_trades-2)" :
                                  "abs(metrics.all.num_trades-2)");
                auto command = ordinary_command(self, plugin, csv, result);
                command.insert(command.begin() + 1, "native-entry");
                command.insert(command.end(), {"--max-trials", "3", "--direction", direction,
                    "--objective", objective, "--trials-out", retention, "--best-k", "1",
                    "--trials-file", stream.string(), "--fixed-input", "SequenceCurveEquity",
                    "100,101;100,101;,", "--proof-curves", proof.string()});
                if (constrained)
                    command.insert(command.end(), {"--constraint", "metrics.all.num_trades<3"});
                const auto row = run_child(root, label, command,
                    {plugin, manifest, csv, result, stream, proof});
                require(row.code == 0 && read_file(row.errors).empty(),
                        "selection CLI changed exit/stderr");
                std::vector<detail::Json> terminal;
                std::istringstream lines(read_file(stream));
                std::string line;
                while (std::getline(lines, line))
                    terminal.push_back(detail::parse_json(line));
                require(terminal.size() == 3, "selection fix dropped streamed terminal rows");
                for (std::size_t index = 0; index < terminal.size(); ++index)
                    require(detail::json_integer(detail::field(terminal[index], "trial_id")) ==
                                static_cast<std::int64_t>(index),
                            "selection stream changed trial ids/order");
                const auto& failed = terminal[1];
                const double failed_score = constrained ? (maximize ? 2 : -2) : 0;
                require(detail::field(failed, "status").text() == "trial_error" &&
                            detail::field(failed, "feasible").value == "true" &&
                            detail::field(failed, "objective").real() == failed_score &&
                            detail::field(failed, "error").text() ==
                                "completed proof trial has no original equity curve",
                        "fixture did not reach the real post-feasibility exception");
                require(detail::field(terminal[2], "status").text() ==
                            (constrained ? "constraint_violation" : "ok"),
                        "selection constraint/tie fixture is wrong");
                const auto document = detail::parse_json(read_file(row.output));
                require(detail::json_integer(detail::field(document, "best_trial_id")) == 0 &&
                            detail::field(document, "best_value").real() ==
                                (constrained ? (maximize ? 1 : -1) : (maximize ? -1 : 1)),
                        "ineligible row won selection or a tie did not prefer the lowest id");
                const auto& trials = detail::field(document, "trials").items;
                if (retention == "all") {
                    require(trials.size() == 3 &&
                                detail::field(trials[1], "status").text() == "trial_error",
                            "all retention lost the error row");
                } else {
                    require(trials.size() == (retention == "none" ? 0U : 1U),
                            "bounded trial retention changed its row count");
                    const auto& summary = detail::field(document, "summary");
                    const auto& best = detail::field(summary, "best_k").items;
                    const auto& counts = detail::field(summary, "counts_by_status");
                    require(best.size() == 1 &&
                                detail::json_integer(detail::field(best[0], "trial_id")) == 0 &&
                                detail::field(best[0], "status").text() == "ok" &&
                                detail::json_integer(detail::field(counts, "trial_error")) == 1,
                            "bounded selection kept an error or stopped counting terminal rows");
                }
                require(read_file(row.output) == read_file(result),
                        "selection file/stdout bytes differ");
                row_verdict(row, "PASS",
                            "error row retained; selection obeys status, constraints and ties");
            }
        }
    }

    // ChartBarsTest adds sum_{v=1..9}(16*v+20)=900 to profit; num_trades remains Length.
    // Grid ids 0,1 have Length 1,2: abs(1-1.5)=abs(2-1.5)=0.5 exactly. Delay id=0 by
    // 2000 ms so id=1 finishes first; assert the recorded order to exercise replacement.
    const auto tie_result = root / "selection-out-of-order-tie.json";
    const auto tie = run_child(root, "selection-out-of-order-tie",
        {self.string(), "native-entry", "run", "--strategy", plugin.string(),
         "--ohlcv", csv.string(), "--output", tie_result.string(),
         "--objective", "abs(metrics.all.num_trades-1.5)", "--sampler", "grid", "--seed", "17",
         "--workers", "2", "--schedule", "rolling", "--max-trials", "2",
         "--input-tf", "1", "--script-tf", "5", "--bar-magnifier", "true",
         "--magnifier-samples", "6", "--magnifier-distribution", "triangle",
         "--int-dim", "Length", "1", "2", "1", "--fixed-input", "BatchPrefixTest", "1",
         "--fixed-input", "BatchPrefixJitter", "0", "--fixed-input", "ChartBarsTest", "1",
         "--fixed-input", "DelayAtLength", "1", "--fixed-input", "DelayAtLengthMs", "2000"},
        {plugin, manifest, csv, tie_result});
    require(tie.code == 0 && read_file(tie.errors).empty(), "out-of-order tie changed exit/stderr");
    const auto tie_document = detail::parse_json(read_file(tie.output));
    const auto& tie_order = detail::field(tie_document, "completion_order").items;
    const auto& tied_trials = detail::field(tie_document, "trials").items;
    require(tie_order.size() == 2 && tied_trials.size() == 2 &&
                detail::json_integer(tie_order[0]) == 1 && detail::json_integer(tie_order[1]) == 0,
            "tie fixture did not finish the higher id first");
    for (const auto& trial : tied_trials)
        require(detail::field(trial, "status").text() == "ok" &&
                    detail::field(trial, "objective").real() == 0.5,
                "out-of-order tie scores disagree with fixture arithmetic");
    require(detail::json_integer(detail::field(tie_document, "best_trial_id")) == 0 &&
                detail::field(tie_document, "best_value").real() == 0.5,
            "equal-score replacement did not prefer the lower trial id");
    row_verdict(tie, "PASS", "higher id finished first with score 0.5; lower id won the tie");

    // (d) Each child admits the dataset/plugin once, then invokes run_study sequentially in the
    // SAME process. False pass: one CLI subprocess per study, checking fds before teardown only,
    // measuring the parent/wrapper, or a memory checker that also accepts actual retained pages.
    const auto lifecycle = [&](const std::string& label, int count, const std::string& control) {
        return run_child(root, label,
            {self.string(), "child-lifecycle", plugin.string(), csv.string(),
             std::to_string(count), control}, {plugin, manifest, csv});
    };
    const auto fd_control = lifecycle("fd-control", 50, "fd");
    require(fd_control.code == 0, "fd planted-control work failed");
    const auto fd_document = detail::parse_json(read_file(fd_control.output));
    check_lifecycle_document(fd_document, 50);
    const auto baseline_fds = detail::json_integer(detail::field(fd_document, "baseline_fds"));
    const auto& retained_fds = detail::field(fd_document, "fds").items;
    for (std::size_t index = 0; index < retained_fds.size(); ++index)
        require(detail::json_integer(retained_fds[index]) ==
                    baseline_fds + static_cast<std::int64_t>(index) + 1,
                "fd control did not actually retain one descriptor per study");
    must_reject([&] { check_fds(fd_document, 50); }, "fd checker accepted 50 retained descriptors");
    row_verdict(fd_control, "PASS", "fd checker rejected actual descriptor retention");
    const auto one = lifecycle("lifecycle-1", 1, "none");
    require(one.code == 0, "one-study work failed");
    check_fds(detail::parse_json(read_file(one.output)), 1);
    row_verdict(one, "PASS", "one-study baseline completed and released every study descriptor");
    const auto many = lifecycle("lifecycle-50", 50, "none");
    require(many.code == 0, "50-study work failed");
    check_fds(detail::parse_json(read_file(many.output)), 50);
#ifdef PINEFORGE_HPO_STUDY_RSS_GATE
    check_rss(one, many);
    const auto rss_control = lifecycle("rss-control", 50, "rss");
    require(rss_control.code == 0, "RSS planted-control work failed");
    check_fds(detail::parse_json(read_file(rss_control.output)), 50);
    require(rss_control.peak_bytes > 0 && one.peak_bytes > 0,
            "RSS control is missing the work's wait4 usage");
    must_reject([&] { check_rss(one, rss_control); }, "RSS checker accepted 50 MiB retained pages");
    row_verdict(rss_control, "PASS",
                "32 bytes/trial checker rejected actual touched-page retention");
    write_file(root / "memory.result.json",
        "{\"verdict\":\"PASS\",\"studies\":[1,50],\"trials_per_study\":1000,"
        "\"limit_bytes_per_trial\":32,\"peak_rss_bytes\":[" +
        std::to_string(one.peak_bytes) + "," + std::to_string(many.peak_bytes) + "]}\n");
    row_verdict(many, "PASS", "50 studies: constant fds and RSS slope within 32 bytes/trial");
#else
    row_verdict(many, "PASS", "50 studies: constant fds; Linux release CI owns the RSS gate");
#endif
}

}  // namespace

int main(int argc, char** argv) {
    Path root;
    try {
        if (argc >= 2 && std::string(argv[1]) == "run") {
            guard_native_arguments(argc, argv);
            return pineforge_hpo_native_entry(argc, argv);
        }
        if (argc >= 2 && std::string(argv[1]) == "native-entry") {
            guard_native_arguments(argc - 1, argv + 1);
            return pineforge_hpo_native_entry(argc - 1, argv + 1);
        }
        if (argc == 2 && std::string(argv[1]) == "child-crash") {
            ::raise(SIGKILL);
            return 99;
        }
        if (argc == 4 && (std::string(argv[1]) == "child-view" ||
                          std::string(argv[1]) == "child-view-blocks")) {
            child_view(checked(argv[2]), checked(argv[3]),
                       std::string(argv[1]) == "child-view-blocks");
            return 0;
        }
        if (argc == 6 && std::string(argv[1]) == "child-lifecycle") {
            child_lifecycle(checked(argv[2]), checked(argv[3]), std::stoi(argv[4]), argv[5]);
            return 0;
        }
        require(argc == 4, "usage: test_study_split NATIVE PLUGIN ARTIFACT_ROOT");
        const auto artifact_root = checked(argv[3]);
        files("artifact directory", {artifact_root});
        std::filesystem::create_directories(artifact_root);
        root = artifact_root / ("run-" + std::to_string(::getpid()) + "-" +
            std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
        files("new retained run directory", {root});
        require(std::filesystem::create_directory(root), "run directory already exists");
        suite(checked(argv[0]), checked(argv[1]), checked(argv[2]), root);
        write_file(root / "RUN.COMPLETE", "{\"verdict\":\"PASS\",\"tests\":["
            "\"crash\",\"ordinary-bytes\",\"view-study\",\"selection\",\"lifecycle\"]}\n");
        write_file(root / "suite.exit", "0\n");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "study-split verdict: FAIL: " << error.what() << '\n';
        if (!root.empty()) {
            write_file(root / "RUN.COMPLETE",
                "{\"verdict\":\"FAIL\",\"reason\":" + quoted(error.what()) + "}\n");
            write_file(root / "suite.exit", "1\n");
        }
        return 1;
    }
}
