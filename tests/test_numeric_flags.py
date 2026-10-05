"""Custom CMake build types must contribute their per-configuration numerical flags."""

from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root, dlib = Path(sys.argv[1]), Path(sys.argv[2])
    hashes = []
    with tempfile.TemporaryDirectory() as temporary:
        for index, flag in enumerate(("-O1", "-O2")):
            build = Path(temporary) / str(index)
            subprocess.run(
                [
                    "cmake",
                    "-S",
                    str(root),
                    "-B",
                    str(build),
                    "-DCMAKE_BUILD_TYPE=FeedAudit",
                    f"-DCMAKE_CXX_FLAGS_FEEDAUDIT={flag}",
                    "-DPINEFORGE_HPO_BUILD_TESTS=OFF",
                    "-DPINEFORGE_HPO_BUILD_NATIVE_CLI=OFF",
                    "-DPINEFORGE_HPO_BUILD_ENGINE_ADAPTER=OFF",
                    f"-DFETCHCONTENT_SOURCE_DIR_DLIB={dlib}",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
            )
            subprocess.run(
                [
                    "cmake",
                    "--build",
                    str(build),
                    "--target",
                    "pineforge_hpo_numeric_flags",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                timeout=30,
            )
            flags = build / "generated/FeedAudit/CXX/numeric_build_flags.txt"
            if flag not in flags.read_text():
                raise AssertionError("custom per-config flags were not included")
            hashes.append(
                (build / "generated/FeedAudit/CXX/numeric_build_flags.hpp").read_text()
            )
    if hashes[0] == hashes[1]:
        raise AssertionError(
            "custom per-config flag change did not change numerical hash"
        )
    print("PASS: FeedAudit -O1/-O2 produce different flags hashes")


if __name__ == "__main__":
    main()
