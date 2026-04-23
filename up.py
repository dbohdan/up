#! /usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 D. Bohdan
# SPDX-License-Identifier: MIT
#
# Upload files to your server using SFTP and get their public URLs.
# https://github.com/dbohdan/up
#
# Requires Python 3.11 and sftp(1).

import argparse
import contextlib
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
from typing import NoReturn, Self


class CollisionError(Exception):
    """Raised when two uploads would resolve to the same basename."""


class ConfigError(Exception):
    """Raised when the configuration file cannot be loaded or is invalid."""


@dataclass(frozen=True)
class Config:
    base_url: str
    dest_dir: Path
    target_host: str

    @classmethod
    def load_config(cls) -> Self:
        # Parse the config file.
        config_home = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        config_file = Path(config_home) / "up" / "config.toml"

        try:
            with config_file.open("rb") as f:
                config = tomllib.load(f)

        except FileNotFoundError:
            msg = f"config file not found at {str(config_file)!r}"
            raise ConfigError(
                msg,
            ) from None

        except PermissionError:
            msg = f"permission denied reading config file at {str(config_file)!r}"
            raise ConfigError(
                msg,
            ) from None

        except OSError as e:
            msg = f"cannot read config file at {str(config_file)!r}: {e}"
            raise ConfigError(
                msg,
            ) from None

        except tomllib.TOMLDecodeError as e:
            msg = f"invalid TOML in config file at {str(config_file)!r}: {e}"
            raise ConfigError(
                msg,
            ) from None

        def require_str(key: str) -> str:
            try:
                value = config[key]
            except KeyError:
                msg = f"missing config key: {key!r}"
                raise ConfigError(msg) from None

            if not isinstance(value, str):
                msg = f"config key {key!r} must be a string, got {type(value).__name__}"
                raise ConfigError(
                    msg,
                )

            return value

        return cls(
            base_url=require_str("base-url").rstrip("/"),
            dest_dir=Path(require_str("dest-dir")),
            target_host=require_str("target-host"),
        )


@dataclass(frozen=True)
class UploadPlan:
    subdir: Path
    basenames: list[str]
    batch: list[str]
    urls: list[str]


def log_error(message: str) -> None:
    me = Path(sys.argv[0]).name
    print(f"{me}: error: {message}", file=sys.stderr)


def fail(message: str) -> NoReturn:
    log_error(message)
    sys.exit(1)


def base32_crockford(a: int) -> str:
    if a == 0:
        return "0"

    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    result = []

    while a > 0:
        result.append(alphabet[a % 32])
        a //= 32

    return "".join(result[::-1])


def random_name() -> str:
    """Generate a unique subdirectory name."""
    timestamp = int(time.time())
    random = secrets.randbits(25)  # Five base32 characters.

    return f"{base32_crockford(timestamp)}-{base32_crockford(random):>05s}"


def slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9._~+-]+", "-", s)

    return s.strip("-") or s


def strip_exif(srcs: list[Path], temp_dir: Path) -> list[Path]:
    """Copy files to per-index subdirectories
    and strip Exif tags in one exiftool(1) call."""
    dests = []
    for i, src in enumerate(srcs):
        subdir = temp_dir / f"{i:02}"
        subdir.mkdir()
        dest = subdir / src.name
        shutil.copy(src, dest)
        dests.append(dest)

    if not dests:
        return dests

    try:
        sp.run(
            [
                "exiftool",
                "-all=",
                "-overwrite_original",
                "-quiet",
                *[str(d) for d in dests],
            ],
            check=True,
        )
    except (sp.CalledProcessError, FileNotFoundError) as e:
        fail(f"failed to strip Exif tags: {e}")

    return dests


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload files and print their URLs.")
    parser.add_argument(
        "files",
        metavar="file",
        nargs="+",
        type=Path,
        help="files to upload; use '-' to read one from stdin",
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
        help="override filename, skipping slugification (one use is one file in order)",
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

    # Validate the permissions string.
    if args.permissions and not re.fullmatch(r"[0-7]+", args.permissions):
        parser.error(
            f"invalid permissions: {args.permissions!r} (expected octal digits)",
        )

    # Validate that stdin is requested at most once.
    if sum(1 for f in args.files if str(f) == "-") > 1:
        parser.error("'-' (stdin) may be used at most once")

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
) -> UploadPlan:
    """Build an sftp batchfile and the list of resulting URLs."""
    batch = []
    urls = []
    basenames: list[str] = []
    seen: set[str] = set()
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

        if basename in seen:
            msg = (
                f"two uploads resolve to the same name {basename!r}; "
                "use -f to disambiguate"
            )
            raise CollisionError(
                msg,
            )
        seen.add(basename)
        basenames.append(basename)

        basename_quoted = shlex.quote(basename)

        batch.append(f"put {file_path_quoted} {basename_quoted}")
        if permissions:
            batch.append(f"chmod {permissions} {basename_quoted}")

        basename_url = urllib.parse.quote(basename, safe="")
        urls.append(f"{base_url}/{subdir.name}/{basename_url}")

    return UploadPlan(
        subdir=subdir,
        basenames=basenames,
        batch=batch,
        urls=urls,
    )


def build_cleanup_batch(plan: UploadPlan) -> list[str]:
    """Build a best-effort batch
    that removes any uploaded files and the subdir on failure."""
    subdir_quoted = shlex.quote(str(plan.subdir))
    # The "-" prefix makes sftp ignore per-command errors.
    lines = [f"-rm {subdir_quoted}/{shlex.quote(b)}" for b in plan.basenames]
    lines.append(f"-rmdir {subdir_quoted}")

    return lines


def main() -> None:
    args = cli()

    try:
        config = Config.load_config()
    except ConfigError as e:
        fail(str(e))

    uses_stdin = any(str(f) == "-" for f in args.files)
    if uses_stdin and sys.stdin.isatty():
        fail(
            "refusing to read upload data from a terminal; "
            "pipe into '-' or redirect a file",
        )

    # Validate all files (except stdin) first by trying to open them.
    success = True

    for file_path in args.files:
        if str(file_path) == "-":
            continue
        try:
            with file_path.open("rb"):
                pass
        except OSError as e:
            log_error(f"cannot open for reading {str(file_path)!r}: {e}")
            success = False

    if not success:
        sys.exit(1)

    with contextlib.ExitStack() as stack:
        files_to_upload = list(args.files)

        if uses_stdin:
            stdin_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stdin_path = stdin_dir / "stdin.txt"

            with stdin_path.open("wb") as f:
                shutil.copyfileobj(sys.stdin.buffer, f)

            files_to_upload = [
                stdin_path if str(f) == "-" else f for f in files_to_upload
            ]

        if args.strip_exif:
            temp_path = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            files_to_upload = strip_exif(files_to_upload, temp_path)

        # Compose a batchfile for sftp.
        subdir = config.dest_dir / random_name()

        try:
            plan = build_sftp_batch(
                files_to_upload,
                base_url=config.base_url,
                filename_overrides=args.filename,
                permissions=args.permissions,
                slugify=args.slug,
                subdir=subdir,
            )
        except CollisionError as e:
            fail(str(e))

        # Run sftp with the batchfile read from stdin.
        sftp_result = sp.run(
            ["sftp", "-b", "-", "-p", config.target_host],
            input="\n".join(plan.batch).encode(),
            check=False,
        )

        if sftp_result.returncode != 0:
            # Best-effort cleanup of any partial upload.
            cleanup = build_cleanup_batch(plan)

            sp.run(
                ["sftp", "-b", "-", config.target_host],
                input="\n".join(cleanup).encode(),
                check=False,
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )

            fail(f"sftp failed with exit code {sftp_result.returncode}")

    print("\n".join(plan.urls))


if __name__ == "__main__":
    main()
