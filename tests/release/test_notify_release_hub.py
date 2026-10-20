"""Producer contract and fail-closed replay tests for CI/cloud only."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import io
from itertools import product
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
NOTIFY = importlib.import_module("notify_release_hub")
FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/producer-events.json").read_text()
)
COMMIT = "1" * 40
READ_TOKEN = str(mock.sentinel.read_credential)
HUB_TOKEN = str(mock.sentinel.hub_credential)


def package_assets(
    version: str,
    *,
    metadata_version: str | None = None,
    stamp_overrides: dict | None = None,
) -> dict[str, bytes]:
    """Real zip/tar containers with the real catalog and a release-stamped diff."""
    base = f"pineforge_hpo-{version}"
    catalog = (ROOT / "python/pineforge_hpo/hpo_failure_codes.json").read_bytes()
    target = {
        "version": version,
        "catalogSha256": hashlib.sha256(catalog).hexdigest(),
        **(stamp_overrides or {}),
    }
    diff = json.dumps(
        {"schema": "pineforge-hpo-failure-catalog-diff/v1", "to": target}
    ).encode()
    assets = {"hpo_failure_codes.json": catalog, "hpo_failure_codes_diff.json": diff}
    metadata = (
        "Metadata-Version: 2.4\nName: pineforge-hpo\n"
        f"Version: {metadata_version or version}\n\n"
    ).encode()
    wheel_bytes = io.BytesIO()
    with zipfile.ZipFile(wheel_bytes, "w") as wheel:
        wheel.writestr(base + ".dist-info/METADATA", metadata)
        for name, payload in assets.items():
            wheel.writestr("pineforge_hpo/" + name, payload)
    sdist_bytes = io.BytesIO()
    with tarfile.open(fileobj=sdist_bytes, mode="w:gz") as sdist:
        files = {"PKG-INFO": metadata, "VERSION": (version + "\n").encode()}
        files.update(
            {"python/pineforge_hpo/" + name: data for name, data in assets.items()}
        )
        for name, payload in files.items():
            member = tarfile.TarInfo(base + "/" + name)
            member.size = len(payload)
            sdist.addfile(member, io.BytesIO(payload))
    assets[base + "-py3-none-any.whl"] = wheel_bytes.getvalue()
    assets[base + ".tar.gz"] = sdist_bytes.getvalue()
    return assets


class Response(io.BytesIO):
    def __init__(self, payload: bytes, status: int):
        super().__init__(payload)
        self.status = status


class GitHubFixture:
    """Exercise the real urllib Request boundary; reject every unexpected mutation."""

    def __init__(self, fixture: dict):
        self.release = copy.deepcopy(fixture["release"])
        self.version = fixture["expected_dispatch"]["client_payload"]["version"]
        self.tag = self.release["tag_name"]
        self.source = "https://api.github.com/repos/pineforge-4pass/pineforge-hpo/"
        self.dispatch_url = (
            "https://api.github.com/repos/pineforge-4pass/pineforge-release/dispatches"
        )
        self.ref = {
            "ref": "refs/tags/" + self.tag,
            "object": {"type": "commit", "sha": COMMIT},
        }
        self.tag_objects = {}
        self.version_file = (self.version + "\n").encode()
        self.assets = package_assets(self.version)
        self.refresh_inventory()
        self.calls = []
        self.posts = []
        self.failures = {}
        self.dispatch_status = 204

    def asset_url(self, name):
        return (
            "https://github.com/pineforge-4pass/pineforge-hpo/releases/download/"
            + self.tag
            + "/"
            + name
        )

    def refresh_inventory(self):
        self.inventory = [
            {
                "name": name,
                "state": "uploaded",
                "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "browser_download_url": self.asset_url(name),
            }
            for name, payload in self.assets.items()
        ]

    def __call__(self, request):
        url, method = request.full_url, request.get_method()
        authorization = request.get_header("Authorization")
        self.calls.append((method, url, authorization))
        if method == "POST":
            if url != self.dispatch_url or authorization != "Bearer " + HUB_TOKEN:
                raise AssertionError("dispatch must use only the scoped hub credential")
            self.posts.append(json.loads(request.data))
        elif url.startswith(self.source):
            if method != "GET" or authorization != "Bearer " + READ_TOKEN:
                raise AssertionError("source API requests must use the read credential")
        elif method != "GET" or authorization is not None:
            raise AssertionError("public assets must be downloaded without credentials")
        if status := self.failures.get(url):
            raise urllib.error.HTTPError(
                url, status, "fixture failure", {}, io.BytesIO()
            )
        if method == "POST":
            return Response(b"", self.dispatch_status)
        if url == self.source + "releases/tags/" + self.tag:
            payload = self.release
        elif url == self.source + "git/ref/tags/" + self.tag:
            payload = self.ref
        elif url.startswith(self.source + "git/tags/"):
            payload = self.tag_objects[url.rsplit("/", 1)[1]]
        elif url == self.source + f"contents/VERSION?ref={COMMIT}":
            return Response(self.version_file, 200)
        elif url.startswith(self.source + f"releases/{self.release['id']}/assets?"):
            page = int(url.rsplit("=", 1)[1])
            payload = self.inventory[(page - 1) * 100 : page * 100]
        else:
            for name, payload in self.assets.items():
                if url == self.asset_url(name):
                    return Response(payload, 200)
            raise AssertionError(f"unexpected request: {method} {url}")
        return Response(json.dumps(payload).encode(), 200)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.receipt = self.directory / "receipt.json"
        self.summary = self.directory / "summary.md"

    def invoke(self, api, *, tag=None, run_id="123456", attempt="1", repository=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": repository or "pineforge-4pass/pineforge-hpo",
                    "GITHUB_RUN_ID": run_id,
                    "GITHUB_RUN_ATTEMPT": attempt,
                    "RELEASE_READ_TOKEN": READ_TOKEN,
                    "HUB_DISPATCH_TOKEN": HUB_TOKEN,
                    "GITHUB_STEP_SUMMARY": str(self.summary),
                },
            ),
            mock.patch.object(NOTIFY.urllib.request, "urlopen", side_effect=api),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                status = NOTIFY.main(
                    ["--tag", tag or api.tag, "--receipt", str(self.receipt)]
                )
            except SystemExit as error:
                status = error.code
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_blocked(self, api, **arguments):
        status, stdout, stderr = self.invoke(api, **arguments)
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertIn("HPO hub notification failed", stderr)
        self.assertFalse(self.receipt.exists())
        self.assertNotIn("GitHub accepted", stdout)
        for credential in (READ_TOKEN, HUB_TOKEN):
            self.assertNotIn(credential, stdout + stderr)

    def test_stable_and_rc_dispatch_exact_producer_contract_only_after_downloads(self):
        for fixture in FIXTURES:
            with self.subTest(tag=fixture["release"]["tag_name"]):
                api = GitHubFixture(fixture)
                status, stdout, stderr = self.invoke(api)
                self.assertEqual(status, 0, stdout + stderr)
                self.assertEqual(api.posts, [fixture["expected_dispatch"]])
                payload = api.posts[0]["client_payload"]
                self.assertIs(type(payload["prerelease"]), bool)
                self.assertIs(type(payload["run_id"]), int)
                self.assertEqual(api.calls[-1][:2], ("POST", api.dispatch_url))
                downloads = [url for method, url, _ in api.calls if "download/" in url]
                self.assertEqual(set(downloads), {api.asset_url(n) for n in api.assets})
                receipt = json.loads(self.receipt.read_text())
                self.assertEqual(receipt["state"], "dispatch_accepted")
                self.assertEqual(receipt["release"]["commit"], COMMIT)
                self.assertEqual(
                    receipt["release"]["assets"],
                    {asset["name"]: asset["digest"] for asset in api.inventory},
                )
                self.assertIn("readiness are pending", self.summary.read_text())

    def test_duplicate_and_missed_event_replay_rechecks_the_same_release_bytes(self):
        api = GitHubFixture(FIXTURES[0])
        for run_id, attempt in (("123456", "1"), ("123456", "2"), ("123457", "1")):
            with self.subTest(run_id=run_id, attempt=attempt):
                status, stdout, stderr = self.invoke(
                    api, run_id=run_id, attempt=attempt
                )
                self.assertEqual(status, 0, stdout + stderr)
                receipt = json.loads(self.receipt.read_text())
                self.assertEqual(receipt["run_attempt"], int(attempt))
                self.assertEqual(receipt["release"]["tag"], "v1.6.0")
        self.assertEqual(api.posts[0], api.posts[1])
        expected_replay = copy.deepcopy(api.posts[0])
        expected_replay["client_payload"]["run_id"] = 123457
        self.assertEqual(api.posts[2], expected_replay)
        self.assertEqual(sum(method == "GET" for method, _, _ in api.calls), 3 * 8)

    def test_missing_each_required_asset_is_red_and_emits_no_event(self):
        # Refuse removal of the asset gate or dispatch before validation.
        for missing in package_assets("1.6.0"):
            with self.subTest(missing=missing):
                api = GitHubFixture(FIXTURES[0])
                api.inventory = [a for a in api.inventory if a["name"] != missing]
                self.assert_blocked(api)
                self.assertEqual(api.posts, [])

    def test_upload_state_size_digest_duplicates_and_source_identity_block_dispatch(
        self,
    ):
        mutations = (
            {"state": "starter"},
            {"size": 0},
            {"size": True},
            {"digest": "sha256:" + "0" * 64},
            {"browser_download_url": "https://example.invalid/elsewhere.whl"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                api = GitHubFixture(FIXTURES[0])
                api.inventory[0].update(mutation)
                self.assert_blocked(api)
                self.assertEqual(api.posts, [])
        api = GitHubFixture(FIXTURES[0])
        api.inventory.append(copy.deepcopy(api.inventory[0]))
        self.assert_blocked(api)
        self.assertEqual(api.posts, [])

    def test_invalid_tag_spellings_are_rejected_before_any_network_request(self):
        for tag in (
            "1.6.0",
            "v1.6.0-rc.1",
            "v1.6.0rc0",
            "v1.6.0rc01",
            "v1.6.0RC1",
            "v01.6.0",
            "v1.6.00",
            "v1.6.0a1",
            "v1.6.0b1",
            "v1.6.0+local",
            "v1.6.0.post1",
            "v1.6.0\n",
            "v1.6.0/../../main",
            "v1.6.0$(false)",
        ):
            with self.subTest(tag=tag):
                api = GitHubFixture(FIXTURES[0])
                self.assert_blocked(api, tag=tag)
                self.assertEqual(api.calls, [])

    def test_draft_unpublished_wrong_tag_and_contradictory_prerelease_are_red(self):
        for mutation in (
            {"draft": True},
            {"draft": "false"},
            {"published_at": None},
            {"tag_name": "v1.6.1"},
            {"prerelease": True},
            {"prerelease": "false"},
            {"id": True},
        ):
            with self.subTest(mutation=mutation):
                api = GitHubFixture(FIXTURES[0])
                api.release.update(mutation)
                self.assert_blocked(api)
                self.assertEqual(api.posts, [])

    def test_tagged_version_and_repository_must_agree(self):
        for version_bytes in (b"1.6.0rc1\n", b"1.6.0 \n", b"1.6.0\n\n"):
            with self.subTest(version_bytes=version_bytes):
                api = GitHubFixture(FIXTURES[0])
                api.version_file = version_bytes
                self.assert_blocked(api)
                self.assertEqual(api.posts, [])
        api = GitHubFixture(FIXTURES[0])
        self.assert_blocked(api, repository="someone/pineforge-hpo")
        self.assertEqual(api.calls, [])

    def test_annotated_tags_resolve_to_full_commits(self):
        api = GitHubFixture(FIXTURES[0])
        tag_sha = "2" * 40
        api.ref["object"] = {"type": "tag", "sha": tag_sha}
        api.tag_objects[tag_sha] = {"object": {"type": "commit", "sha": COMMIT}}
        status, stdout, stderr = self.invoke(api)
        self.assertEqual(status, 0, stdout + stderr)
        self.assertEqual(
            json.loads(self.receipt.read_text())["release"]["commit"], COMMIT
        )

    def test_wrong_source_refs_short_commits_and_tag_cycles_are_red(self):
        for mutation in (
            {"ref": "refs/heads/v1.6.0"},
            {"object": {"type": "commit", "sha": "1234567"}},
            {"object": {"type": "tree", "sha": COMMIT}},
            {"object": {"type": "tag", "sha": COMMIT}},
        ):
            with self.subTest(mutation=mutation):
                api = GitHubFixture(FIXTURES[0])
                api.ref.update(mutation)
                api.tag_objects[COMMIT] = {"object": {"type": "tag", "sha": COMMIT}}
                self.assert_blocked(api)
                self.assertEqual(api.posts, [])

    def test_asset_inventory_is_paginated(self):
        api = GitHubFixture(FIXTURES[0])
        api.inventory = [{"name": f"unrelated-{i}"} for i in range(100)] + api.inventory
        status, stdout, stderr = self.invoke(api)
        self.assertEqual(status, 0, stdout + stderr)
        self.assertTrue(any("page=2" in url for _, url, _ in api.calls))

    def test_wrong_package_metadata_and_corrupted_catalogs_are_red(self):
        for corruption in ("metadata", "wheel", "catalog", "stamp"):
            with self.subTest(corruption=corruption):
                api = GitHubFixture(FIXTURES[0])
                if corruption == "metadata":
                    api.assets = package_assets("1.6.0", metadata_version="1.6.0rc1")
                elif corruption == "wheel":
                    api.assets["pineforge_hpo-1.6.0-py3-none-any.whl"] = b"not a zip"
                elif corruption == "catalog":
                    api.assets["hpo_failure_codes.json"] += b"\n"
                else:
                    api.assets = package_assets(
                        "1.6.0", stamp_overrides={"version": "1.6.0rc1"}
                    )
                api.refresh_inventory()  # Superficial size/hash checks still pass.
                self.assert_blocked(api)
                self.assertEqual(api.posts, [])

    def test_transport_and_authentication_errors_never_write_an_acceptance_receipt(
        self,
    ):
        for stage in ("release", "inventory", "asset", "dispatch"):
            for status in (401, 403, 404, 500):
                with self.subTest(stage=stage, status=status):
                    api = GitHubFixture(FIXTURES[0])
                    urls = {
                        "release": api.source + "releases/tags/" + api.tag,
                        "inventory": api.source
                        + f"releases/{api.release['id']}/assets?per_page=100&page=1",
                        "asset": api.asset_url("hpo_failure_codes.json"),
                        "dispatch": api.dispatch_url,
                    }
                    api.failures[urls[stage]] = status
                    self.assert_blocked(api)
                    self.assertEqual(len(api.posts), int(stage == "dispatch"))

    def test_unexpected_dispatch_status_is_red(self):
        api = GitHubFixture(FIXTURES[0])
        api.dispatch_status = 202
        self.assert_blocked(api)


class WorkflowWiringTests(unittest.TestCase):
    def setUp(self):
        # BaseLoader retains Actions' `on` key and avoids YAML 1.1 boolean coercion.
        self.workflow = yaml.load(
            (ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader
        )

    def assert_release_runners(self, workflow):
        for event in ("pull_request", "pull_request_target"):
            self.assertNotIn(event, workflow["on"])
        for job_id in ("tag-guard", "package", "notify-hub"):
            self.assertEqual(
                workflow["jobs"][job_id]["runs-on"],
                ["self-hosted", "pf-ci", "pf-ci-release"],
            )

    def test_release_jobs_use_isolated_runners_without_pr_triggers(self):
        self.assert_release_runners(self.workflow)

    def test_hosted_labels_and_release_runner_fallbacks_are_rejected(self):
        bad_runners = (
            "ubuntu-24.04",
            "ubuntu-latest",
            ["self-hosted", "pf-ci", "pf-ci-x64"],
            "${{ fromJSON('[\"self-hosted\",\"pf-ci\",\"pf-ci-release\"]') "
            "|| 'ubuntu-24.04' }}",
        )
        for job_id in ("tag-guard", "package", "notify-hub"):
            for runner in bad_runners:
                with self.subTest(job=job_id, runner=runner):
                    mutated = copy.deepcopy(self.workflow)
                    mutated["jobs"][job_id]["runs-on"] = runner
                    with self.assertRaises(AssertionError):
                        self.assert_release_runners(mutated)

    def test_release_runners_reject_pr_triggers_even_with_branch_filters(self):
        for event in ("pull_request", "pull_request_target"):
            for trigger in ("", {"branches": ["main"]}):
                with self.subTest(event=event, trigger=trigger):
                    mutated = copy.deepcopy(self.workflow)
                    mutated["on"][event] = trigger
                    with self.assertRaises(AssertionError):
                        self.assert_release_runners(mutated)

    def assert_package_and_replay(self, workflow):
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(
            workflow["on"]["workflow_dispatch"]["inputs"]["tag"]["required"],
            "true",
        )
        jobs = workflow["jobs"]
        self.assertEqual(jobs["package"]["needs"], "tag-guard")
        package_expression = " ".join(
            jobs["package"]["if"].strip().removeprefix("${{").removesuffix("}}").split()
        )
        package_expression = package_expression.replace("&&", "and")
        package_expression = re.sub(r"!(?!=)", "not ", package_expression)
        job = jobs["notify-hub"]
        self.assertEqual(job["needs"], ["tag-guard", "package"])
        expression = " ".join(
            job["if"].strip().removeprefix("${{").removesuffix("}}").split()
        )
        expression = expression.replace("&&", "and").replace("||", "or")
        expression = re.sub(r"!(?!=)", "not ", expression)
        expression = expression.replace("needs.tag-guard", "needs.tag_guard")
        results = ("success", "failure", "cancelled", "skipped")
        for event, ref, package, result, guard, cancelled in product(
            ("release", "workflow_dispatch", "push"),
            ("refs/heads/main", "refs/heads/side", "refs/tags/v1.6.0"),
            (False, True),
            results,
            results,
            (False, True),
        ):
            context = {
                "github": SimpleNamespace(event_name=event, ref=ref),
                "inputs": SimpleNamespace(package=package),
                "needs": SimpleNamespace(
                    package=SimpleNamespace(result=result),
                    tag_guard=SimpleNamespace(result=guard),
                ),
                "cancelled": lambda: cancelled,
            }
            detail = (event, ref, package, result, guard, cancelled)
            allowed = eval(expression, {"__builtins__": {}}, context)
            self.assertEqual(
                eval(package_expression, {"__builtins__": {}}, context),
                event == "workflow_dispatch" and ref == "refs/heads/main" and package,
                detail,
            )
            self.assertEqual(
                allowed,
                not cancelled
                and event == "workflow_dispatch"
                and ref == "refs/heads/main"
                and (
                    guard != "success"
                    or (package, result) in ((True, "success"), (False, "skipped"))
                ),
                detail,
            )
        invocations = [
            s for s in job["steps"] if "notify_release_hub.py" in s.get("run", "")
        ]
        self.assertEqual(len(invocations), 1)
        self.assertIn("--tag", invocations[0]["run"])
        self.assertIn("inputs.tag", invocations[0]["env"]["RELEASE_TAG"])

    def test_package_and_replay_use_one_notifier_after_the_correct_prerequisite(self):
        self.assert_package_and_replay(self.workflow)

    def test_package_and_replay_condition_mutations_are_refused(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        mutations = (
            ("&& inputs.package }}", "&& !inputs.package }}"),
            (
                "(inputs.package && needs.package.result == 'success')",
                "(!inputs.package && needs.package.result == 'success')",
            ),
            (
                "needs.package.result == 'success'",
                "needs.package.result == 'failure'",
            ),
            (
                "needs.package.result == 'skipped'",
                "needs.package.result == 'success'",
            ),
            (
                "(needs.tag-guard.result != 'success' ||",
                "(needs.tag-guard.result == 'success' ||",
            ),
        )
        for old, new in mutations:
            with self.subTest(old=old):
                self.assertEqual(text.count(old), 1, "stale condition mutation")
                mutated = text.replace(old, new)
                self.assertNotEqual(mutated, text)
                workflow = yaml.load(mutated, Loader=yaml.BaseLoader)
                with self.assertRaises(AssertionError):
                    self.assert_package_and_replay(workflow)

    def test_dispatch_credential_is_scoped_to_hub_and_failure_is_visible(self):
        job = self.workflow["jobs"]["notify-hub"]
        self.assertEqual(job["permissions"], {"contents": "read"})
        token_steps = [
            s for s in job["steps"] if "create-github-app-token@" in s.get("uses", "")
        ]
        self.assertEqual(len(token_steps), 1)
        settings = token_steps[0]["with"]
        self.assertEqual(settings["owner"], "${{ github.repository_owner }}")
        self.assertEqual(settings["repositories"], "pineforge-release")
        self.assertEqual(settings["permission-contents"], "write")
        self.assertTrue(any(s.get("if") == "failure()" for s in job["steps"]))


if __name__ == "__main__":
    unittest.main()
