#! /usr/bin/env python3
# Upload files to your server using rsync and get their public URLs.
# https://github.com/dbohdan/up
#
# Copyright (c) 2025 D. Bohdan
# MIT License
#
# Requires Python 3.11 and rsync(1).

import argparse
import os
import re
import secrets
import shlex
import subprocess as sp
import sys
import time
import tomllib
import urllib.parse
from pathlib import Path

RSYNC_COMMAND_PREFIX = (
    "rsync",
    "--checksum",
    "--chmod",
    "0644",
    "--mkpath",
    "--progress",
    "--times",
)


def log_error(message: str) -> None:
    me = Path(sys.argv[0]).name
    print(f"{me}: error: {message}", file=sys.stderr)


def base32_crockford(a: int) -> str:
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    result = []

    while a > 0:
        result.append(alphabet[a % 32])
        a //= 32

    return "".join(result[::-1])


def random_name() -> str:
    """Generate a unique subdirectory name."""
    timestamp = time.time_ns() // 1_000_000_000
    random = secrets.randbits(25)  # Five base32 characters.

    return f"{base32_crockford(timestamp)}-{base32_crockford(random):>05s}"


def slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^A-Za-z0-9._~+-]+", "-", s)
    return s.strip("-")


def main():
    parser = argparse.ArgumentParser(description="Upload files and print their URLs.")
    parser.add_argument("files", metavar="file", nargs="+", help="files to upload")
    args = parser.parse_args()

    # Parse the config file.
    config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    config_file = Path(config_home) / "up" / "config.toml"

    if not config_file.is_file():
        log_error(
            "config file not found at " + shlex.quote(str(config_file)),
        )
        sys.exit(1)

    with config_file.open("rb") as f:
        config = tomllib.load(f)
    try:
        base_url = config["base_url"].rstrip("/")
        dest_dir = Path(config["dest_dir"])
        target_host = config["target_host"]
    except KeyError as e:
        log_error(f"key missing in config: {e}")
        sys.exit(1)

    success = True
    subdir = dest_dir / random_name()

    # rsync one file at a time.
    # This is slower but allows us to generate custom remote filenames.
    for file_path_str in args.files:
        file_path = Path(file_path_str)
        if not file_path.is_file():
            log_error(f"bad file: {file_path_str}")
            success = False
            continue

        remote_basename = urllib.parse.quote(slug(file_path.name), safe="")

        rsync_command = (
            *RSYNC_COMMAND_PREFIX,
            file_path,
            f"{target_host}:{subdir}/{remote_basename}",
        )
        rsync_result = sp.run(
            rsync_command,
            check=False,
            capture_output=False,
        )

        # Print the URL if rsync is successful; print the exit code if it isn't.
        if rsync_result.returncode == 0:
            print(f"{base_url}/{subdir.name}/{remote_basename}")
        else:
            log_error(
                f"rsync failed with exit code {rsync_result.returncode} "
                f"for {file_path_str!r}",
            )
            success = False

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
