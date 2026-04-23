#! /usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 D. Bohdan
# SPDX-License-Identifier: MIT
#
# Upload files to your server using SFTP and get their public URLs.
# https://github.com/dbohdan/up
#
# Requires Python 3.11 and sftp(1).

import argparse
import os
import re
import secrets
import shlex
import shutil
import subprocess as sp
import sys
import tempfile
import time
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Self


@dataclass(frozen=True)
class Config:
    base_url: str
    dest_dir: Path
    target_host: str

    @classmethod
    def load_config(cls) -> Self:
        # Parse the config file.
        config_home = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        config_file = Path(config_home) / "up" / "config.toml"

        try:
            with config_file.open("rb") as f:
                config = tomllib.load(f)
        except FileNotFoundError:
            log_error(
                f"config file not found at {str(config_file)!r}",
            )
            sys.exit(1)
        except PermissionError:
            log_error(
                f"permission denied reading config file at {str(config_file)!r}",
            )
            sys.exit(1)
        except OSError as e:
            log_error(f"cannot read config file at {str(config_file)!r}: {e}")
            sys.exit(1)

        try:
            base_url = config["base-url"].rstrip("/")
            dest_dir = Path(config["dest-dir"])
            target_host = config["target-host"]
        except KeyError as e:
            log_error(f"key missing in config: {e}")
            sys.exit(1)

        return cls(
            base_url=base_url,
            dest_dir=dest_dir,
            target_host=target_host,
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


def copy_and_strip_exif(src: Path, dest_dir: Path) -> Path:
    """Copy a file to a new location and strip its Exif tags using exiftool(1)."""
    dest = dest_dir / src.name
    shutil.copy(src, dest)

    try:
        sp.run(["exiftool", "-all=", "-quiet", str(dest)], check=True)
    except (sp.CalledProcessError, FileNotFoundError) as e:
        log_error(f"failed to strip tags from {str(src)!r}: {e}")
        sys.exit(1)

    return dest


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload files and print their URLs.")
    parser.add_argument(
        "files",
        metavar="file",
        nargs="+",
        type=Path,
        help="files to upload",
    )
    parser.add_argument(
        "-S",
        "--no-slug",
        action="store_false",
        dest="slug",
        help="do not slugify filenames",
    )
    parser.add_argument(
        "-f",
        "--filename",
        metavar="<filename>",
        action="append",
        default=[],
        help="override filename (one use is one file in order)",
    )
    parser.add_argument(
        "-p",
        "--permissions",
        metavar="<perms>",
        default="0644",
        help="set file permissions (%(default)r by default); skip chmod if empty",
    )
    parser.add_argument(
        "-s",
        "--strip-exif",
        action="store_true",
        help="strip Exif metadata with ExifTool",
    )
    args = parser.parse_args()

    # Validate the number of names.
    if len(args.filename) > len(args.files):
        parser.error(
            f"too many names: {len(args.filename)} names for {len(args.files)} files",
        )

    return args


def build_sftp_batch(
    files_to_upload: list[Path],
    /,
    *,
    base_url: str,
    filename_overrides: list[str],
    permissions: str,
    slugify: bool,
    subdir: Path,
) -> tuple[list[str], list[str]]:
    """Build an sftp batchfile and the list of resulting URLs."""
    batch = []
    urls = []
    subdir_quoted = shlex.quote(str(subdir))

    batch.append(f"mkdir {subdir_quoted}")
    batch.append(f"cd {subdir_quoted}")

    for i, file_path in enumerate(files_to_upload):
        file_path_quoted = shlex.quote(str(file_path))

        if i < len(filename_overrides):
            basename = filename_overrides[i]
        elif slugify:
            basename = slug(file_path.name)
        else:
            basename = file_path.name
        basename_quoted = shlex.quote(basename)

        batch.append(f"put {file_path_quoted} {basename_quoted}")
        if permissions:
            batch.append(f"chmod {permissions} {basename_quoted}")

        basename_url = urllib.parse.quote(basename, safe="")
        urls.append(f"{base_url}/{subdir.name}/{basename_url}")

    return batch, urls


def main():
    args = cli()
    config = Config.load_config()

    # Validate all files first by trying to open them.
    success = True

    for file_path in args.files:
        try:
            with file_path.open("r"):
                pass
        except OSError:
            log_error(f"cannot open for reading: {str(file_path)!r}")
            success = False

    if not success:
        sys.exit(1)

    files_to_upload = args.files
    temp_dir = None
    if args.strip_exif:
        temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(temp_dir.name)
        files_to_upload = [copy_and_strip_exif(f, temp_path) for f in args.files]

    # Compose a batchfile for sftp.
    subdir = config.dest_dir / random_name()
    batch, urls = build_sftp_batch(
        files_to_upload,
        base_url=config.base_url,
        filename_overrides=args.filename,
        permissions=args.permissions,
        slugify=args.slug,
        subdir=subdir,
    )

    # Run sftp with the batchfile read from stdin.
    sftp_result = sp.run(
        ["sftp", "-b", "-", "-p", config.target_host],
        input="\n".join(batch).encode(),
        check=False,
    )

    if sftp_result.returncode != 0:
        log_error(f"sftp failed with exit code {sftp_result.returncode}")
        sys.exit(1)

    print("\n".join(urls))


if __name__ == "__main__":
    main()
