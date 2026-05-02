from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# (local filename, hdfs landing dir, replication factor)
DATASETS = [
    ("customer-profiles.json.gz", "/landing/customer-profiles", 3),
    ("device-fingerprints.csv.gz", "/landing/device-fingerprints", 2),
    ("fraud-reports.json.gz", "/landing/fraud-reports", 3),
    ("merchant-directory.csv.gz", "/landing/merchant-directory", 2),
]


def run(cmd: list[str], why: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, printing the command and its purpose first."""
    print()
    print(f"  $ {shlex.join(cmd)}")
    print(f"    -> {why}")
    result = subprocess.run(cmd, check=check, capture_output=True, text=True)
    if result.stdout.strip():
        for line in result.stdout.rstrip().splitlines():
            print(f"      {line}")
    return result


def nn(*args: str, why: str) -> subprocess.CompletedProcess:
    """Run a command inside the namenode container."""
    return run(["docker", "compose", "exec", "-T", "namenode", *args], why)


def hdfs_exists(path: str) -> bool:
    """Return True if `path` exists in HDFS."""
    result = run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "namenode",
            "hdfs",
            "dfs",
            "-test",
            "-e",
            path,
        ],
        why=f"check whether {path} already exists in HDFS (exit 0 = yes, 1 = no)",
        check=False,
    )
    return result.returncode == 0


def land(filename: str, hdfs_dir: str, rep: int) -> None:
    local = DATA_DIR / filename
    if not local.exists():
        sys.exit(f"missing local file: {local}")

    hdfs_path = f"{hdfs_dir}/{filename}"

    if hdfs_exists(hdfs_path):
        print(f"\n  [skip] {hdfs_path} already exists in HDFS")
    else:
        nn(
            "hdfs",
            "dfs",
            "-mkdir",
            "-p",
            hdfs_dir,
            why=f"create {hdfs_dir} (and any missing parent dirs) in HDFS",
        )
        run(
            ["docker", "compose", "cp", str(local), f"namenode:/tmp/{filename}"],
            why=f"copy {filename} from your host into the namenode container's /tmp",
        )
        nn(
            "hdfs",
            "dfs",
            "-put",
            f"/tmp/{filename}",
            hdfs_dir + "/",
            why=f"stream {filename} from the container's /tmp into {hdfs_dir} via the HDFS write pipeline",
        )
        nn(
            "rm",
            f"/tmp/{filename}",
            why="remove the staged copy from the namenode container's local filesystem",
        )

    # setrep is idempotent — safe to re-apply on every run.
    nn(
        "hdfs",
        "dfs",
        "-setrep",
        str(rep),
        hdfs_dir,
        why=f"set the replication factor of {hdfs_dir} to {rep} (audit/regulatory = 3, default = 2)",
    )


def main() -> None:
    print(f"Landing {len(DATASETS)} dimension datasets into HDFS")
    print(f"Source: {DATA_DIR}")
    for filename, hdfs_dir, rep in DATASETS:
        print(f"\n=== {filename} -> {hdfs_dir} (rep={rep}) ===")
        land(filename, hdfs_dir, rep)
    print("\nDone.")


if __name__ == "__main__":
    main()
