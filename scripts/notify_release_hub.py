"""Verify published HPO assets, then notify the fixed PineForge release hub.

Used after release upload and for workflow_dispatch replay. The upstream producer
contract remains hpo-release {version, prerelease, run_id}; only the hub can emit
the pineforge/release-ready/v1 manifest described in the chain design's section 3.2.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import urllib.error
import urllib.request
import zipfile

from gen_catalog_diff import DIFF_SCHEMA, VERSION, validate_catalog, version_key

SOURCE = "pineforge-4pass/pineforge-hpo"
SOURCE_REPOSITORIES = ("pineforge-4pass/pineforge-hpo", "tunaxis/pineforge-hpo")
HUB = "pineforge-4pass/pineforge-release"
API = "https://api.github.com"
CATALOGS = ("hpo_failure_codes.json", "hpo_failure_codes_diff.json")


def release_version(tag: str) -> tuple[str, bool]:
    if not VERSION.fullmatch(tag):
        raise ValueError("tag must use canonical PEP 440: vN.N.N or vN.N.NrcM")
    return tag[1:], version_key(tag)[3] == 0


def request_bytes(
    url: str, *, token: str = "", data: bytes | None = None, raw: bool = False
) -> bytes:
    headers = {
        "Accept": "application/vnd.github.raw+json"
        if raw
        else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request) as response:
        expected = 204 if data is not None else 200
        if response.status != expected:
            raise ValueError(f"unexpected HTTP {response.status}; expected {expected}")
        return response.read()


def source_json(path: str, token: str) -> object:
    return json.loads(request_bytes(f"{API}/repos/{SOURCE}/{path}", token=token))


def tagged_commit(tag: str, token: str) -> str:
    reference = source_json("git/ref/tags/" + tag, token)
    if reference.get("ref") != "refs/tags/" + tag:
        raise ValueError("unexpected source tag identity")
    target = reference["object"]
    seen = set()
    while True:
        sha = target.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("source tag has no full commit identity")
        if target.get("type") == "commit":
            return sha
        if target.get("type") != "tag" or sha in seen:
            raise ValueError("source tag does not resolve to a commit")
        seen.add(sha)
        target = source_json("git/tags/" + sha, token)["object"]


def check_metadata(payload: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(payload)
    if metadata.get_all("Name") != ["pineforge-hpo"] or metadata.get_all("Version") != [
        version
    ]:
        raise ValueError("package metadata disagrees with the release version/name")


def check_packages(assets: dict[str, bytes], version: str) -> None:
    base = f"pineforge_hpo-{version}"
    with zipfile.ZipFile(io.BytesIO(assets[base + "-py3-none-any.whl"])) as wheel:
        check_metadata(wheel.read(base + ".dist-info/METADATA"), version)
        for name in CATALOGS:
            if wheel.read("pineforge_hpo/" + name) != assets[name]:
                raise ValueError(f"wheel catalog differs from release asset: {name}")
    with tarfile.open(
        fileobj=io.BytesIO(assets[base + ".tar.gz"]), mode="r:gz"
    ) as sdist:

        def read(name: str) -> bytes:
            member = sdist.getmember(base + "/" + name)
            if not member.isfile():
                raise ValueError(f"sdist member is not a file: {name}")
            with sdist.extractfile(member) as contents:
                return contents.read()

        check_metadata(read("PKG-INFO"), version)
        if read("VERSION").removesuffix(b"\n") != version.encode():
            raise ValueError("sdist VERSION disagrees with the release tag")
        for name in CATALOGS:
            if read("python/pineforge_hpo/" + name) != assets[name]:
                raise ValueError(f"sdist catalog differs from release asset: {name}")
    validate_catalog(json.loads(assets[CATALOGS[0]]))
    diff = json.loads(assets[CATALOGS[1]])
    if (
        diff.get("schema") != DIFF_SCHEMA
        or diff.get("to", {}).get("version") != version
        or diff["to"].get("unreleased", False) is not False
        or diff["to"].get("catalogSha256")
        != hashlib.sha256(assets[CATALOGS[0]]).hexdigest()
    ):
        raise ValueError("catalog diff is not stamped for these release bytes")


def verify_release(tag: str, token: str) -> dict:
    version, prerelease = release_version(tag)
    release = source_json("releases/tags/" + tag, token)
    if (
        release.get("tag_name") != tag
        or release.get("draft") is not False
        or type(release.get("prerelease")) is not bool
        or release["prerelease"] != prerelease
        or not release.get("published_at")
    ):
        raise ValueError("release must be published with a consistent prerelease flag")
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("invalid published release ID")
    commit = tagged_commit(tag, token)
    version_bytes = request_bytes(
        f"{API}/repos/{SOURCE}/contents/VERSION?ref={commit}", token=token, raw=True
    )
    if version_bytes.removesuffix(b"\n") != version.encode():
        raise ValueError("tagged VERSION disagrees with the release tag")
    required = {
        f"pineforge_hpo-{version}-py3-none-any.whl",
        f"pineforge_hpo-{version}.tar.gz",
        *CATALOGS,
    }
    listed = []
    page = 1
    while True:
        batch = source_json(
            f"releases/{release_id}/assets?per_page=100&page={page}", token
        )
        if not isinstance(batch, list):
            raise ValueError("invalid release asset inventory")
        listed.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    assets, hashes = {}, {}
    for name in sorted(required):
        matching = [asset for asset in listed if asset.get("name") == name]
        if len(matching) != 1:
            raise ValueError(f"required release asset missing or duplicated: {name}")
        asset = matching[0]
        url = f"https://github.com/{SOURCE}/releases/download/{tag}/{name}"
        if (
            asset.get("state") != "uploaded"
            or type(asset.get("size")) is not int
            or asset["size"] <= 0
            or asset.get("browser_download_url") != url
        ):
            raise ValueError(
                f"release asset is not ready at the expected source: {name}"
            )
        # Download exactly as the public hub does. Never forward a token to asset redirects.
        payload = request_bytes(url)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if len(payload) != asset["size"] or asset.get("digest") not in (None, digest):
            raise ValueError(f"release asset size/digest mismatch: {name}")
        assets[name], hashes[name] = payload, digest
    check_packages(assets, version)
    return {
        "release_id": release_id,
        "tag": tag,
        "commit": commit,
        "version": version,
        "prerelease": prerelease,
        "assets": hashes,
    }


def notify(tag: str, run_id: int, read_token: str, dispatch_token: str) -> dict:
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("run_id must be a positive integer")
    if not read_token or not dispatch_token:
        raise ValueError("release read and hub dispatch credentials are required")
    verified = verify_release(tag, read_token)
    event = {
        "event_type": "hpo-release",
        "client_payload": {
            "version": verified["version"],
            "prerelease": verified["prerelease"],
            "run_id": run_id,
        },
    }
    request_bytes(
        f"{API}/repos/{HUB}/dispatches",
        token=dispatch_token,
        data=json.dumps(event).encode(),
    )
    return {
        "state": "dispatch_accepted",
        "source_repository": SOURCE,
        "target_repository": HUB,
        "release": verified,
        "event": event,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if os.environ.get("GITHUB_REPOSITORY") not in SOURCE_REPOSITORIES:
            raise ValueError("notification is restricted to the HPO source repository")
        attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
        if attempt <= 0:
            raise ValueError("run_attempt must be a positive integer")
        receipt = notify(
            args.tag,
            int(os.environ["GITHUB_RUN_ID"]),
            os.environ["RELEASE_READ_TOKEN"],
            os.environ["HUB_DISPATCH_TOKEN"],
        )
        receipt["run_attempt"] = attempt
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        message = (
            f"GitHub accepted hpo-release for {args.tag} after verifying all four assets. "
            "Hub processing and downstream readiness are pending; this is a delivery receipt."
        )
        print(message)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a", encoding="utf-8") as output:
                output.write(message + "\n")
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        tarfile.TarError,
        zipfile.BadZipFile,
        urllib.error.URLError,
    ) as error:
        parser.exit(1, f"HPO hub notification failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
