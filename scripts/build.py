#!/usr/bin/env python3

"""Build CPython for Android and package it into Magisk modules."""

import logging
import os
import re
import subprocess
import tarfile
import tomllib
from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from itertools import chain
from shutil import rmtree
from subprocess import CompletedProcess
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from wcmatch.pathlib import GL, B, E, N, Path

__version__ = "0.1.0"
__author__ = "PhateValleyman <Jonas.Ned@outlook.com>"
__license__ = "MIT"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
MODULE_DIR = PROJECT_ROOT / "module"
PATCHES_DIR = PROJECT_ROOT / "patches"

BUILD_CONFIG_FILE = PROJECT_ROOT / "build.toml"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PythonConfig:
    """Configuration for the CPython build."""

    sources_url: str

    apply_patches: bool
    hosts: list[str]
    configure_args: list[str]
    configure_env: dict[str, str]
    package: bool


@dataclass(frozen=True)
class ModuleConfig:
    """Configuration for the Magisk module."""

    include_files: list[str]

    debloat: bool
    debloat_patterns: list[str]
    replace_shebangs: bool
    shebang_mapping: dict[str, str]
    strip: bool
    strip_args: list[str]


class ModuleBuilder:
    """Class for building Magisk modules."""

    def __init__(
        self,
        sources: Path,
        python_ver: str,
        python_conf: PythonConfig,
        module_conf: ModuleConfig,
    ) -> None:
        """Initialize ModuleBuilder.

        Args:
            sources: Root directory of CPython source code
            python_ver: Python version string
            python_conf: Python build configuration
            module_conf: Module configuration

        """
        self.sources = sources
        self.python_ver = python_ver
        self.python_conf = python_conf
        self.module_conf = module_conf

        self.module_name, self.module_ver = getprops(
            MODULE_DIR / "module.prop",
            "name",
            "version",
        )
        self.module_files = self._collect_module_files()

        # host names to magisk $ARCH mapping
        self.arch_mapping = {
            "aarch64-linux-android": "arm64",
            "armv7a-linux-androideabi": "arm",
            "arm-linux-androideabi": "arm",
        }

    def _create_python_archive(self, prefix: Path) -> BytesIO:
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as archive:
            archive.add(prefix, prefix.name)
        return buf

    def _collect_module_files(self) -> dict[Path, Path]:
        files = {}
        for path, _, filenames in MODULE_DIR.walk(follow_symlinks=False):
            for file in filenames:
                filepath = path / file
                files[filepath] = filepath.relative_to(MODULE_DIR)
        return files

    def _process_module_prop(self, path: Path, arch: str) -> str:
        lines: list[str] = []

        with path.open() as fin:
            for line in fin:
                if line.startswith("description"):
                    lines.append(
                        f"description=CPython {self.python_ver} for {arch.upper()}",
                    )
                else:
                    lines.append(line.strip())

        return "\n".join(lines)

    def _write_module_file(
        self,
        zip_file: ZipFile,
        path: Path,
        rel_path: Path,
        arch: str,
    ) -> None:
        if path.name == "module.prop":
            zip_file.writestr(str(rel_path), self._process_module_prop(path, arch))
        else:
            zip_file.write(path, rel_path)

    def build(self) -> None:
        """Build Magisk modules for all configured hosts."""
        for host in self.python_conf.hosts:
            logger.info("Building for host: %s", host)

            prefix = self.sources / "cross-build" / host / "prefix"
            arch = self.arch_mapping.get(host, "unknown")

            python_buf = self._create_python_archive(prefix)
            output_path = DIST_DIR / f"{self.module_name}-{self.module_ver}-{arch}.zip"

            with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as zip_out:
                zip_out.writestr(
                    f"python-{self.python_ver}-{arch}.tar.xz",
                    python_buf.getvalue(),
                )
                python_buf.close()

                for path, rel_path in self.module_files.items():
                    self._write_module_file(zip_out, path, rel_path, arch)

                for entry in self.module_conf.include_files:
                    zip_out.write(entry)


def remove_entry(path: Path) -> None:
    """Remove file, symlink or directory at given path.

    Args:
        path: Path to remove

    """
    if not path.exists():
        return

    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        rmtree(path)


def getprops(file: Path, *keys: str, sep: str = "=") -> tuple[str, ...]:
    """Get values for keys from property file.

    Args:
        file: Property file to read
        keys: Keys to extract values for
        sep: Key-value separator (default: "=")

    Returns:
        Tuple of found values in order of keys

    """
    values: list[str] = []

    with file.open() as fin:
        for line in fin:
            if sep not in line:
                continue

            fields = line.split(sep, 1)
            if len(fields) != 2:  # noqa: PLR2004
                continue

            if fields[0].strip() in keys:
                values.append(fields[1].strip())

    return tuple(values)


def run(
    cmd: Sequence[str],
    *,
    log: bool = True,
    env: dict[str, str] | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> CompletedProcess:
    """Run shell command with logging and environment.

    Args:
        cmd: Command to run
        log: Whether to log command (default: True)
        env: Environment variables (default: current env)
        kwargs: Additional args for subprocess.run

    Returns:
        CompletedProcess with command results

    Raises:
        CalledProcessError: If command fails and check=True

    """
    kwargs.setdefault("check", True)

    if env is None:
        env = os.environ.copy()

    if log:
        logger.info("> %s", " ".join(cmd))
    return subprocess.run(cmd, env=env, **kwargs)  # noqa: PLW1510


def download(url: str, output: Path) -> CompletedProcess:
    """Download file from URL with retries using curl.

    Args:
        url: URL to download from
        output: Where to save downloaded file

    Returns:
        CompletedProcess with download results

    Raises:
        CalledProcessError: If download fails after retries

    """
    return run(
        ["curl", "-Lf", "--retry", "5", "--retry-all-errors", "-o", str(output), url],
    )


def apply_patches(sources: Path) -> None:
    """Apply git patches to source code.

    Args:
        sources: Source code directory to patch

    Raises:
        CalledProcessError: If patch application fails

    """
    if not PATCHES_DIR.exists():
        return

    cwd = Path().cwd()
    os.chdir(sources)

    for entry in PATCHES_DIR.iterdir():
        run(["patch", "-p1", "-i", str(entry)])

    os.chdir(cwd)


def is_binary(b: bytes) -> bool:
    """Check if bytes contain binary data.

    Args:
        b: Bytes to check

    Returns:
        True if bytes contain binary data

    """
    textchars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})
    return bool(b.translate(None, textchars))


def init(*, clear: bool) -> None:
    """Init build environment.

    Args:
        clear: Whether to clean build/dist dirs

    Raises:
        OSError: If ANDROID_HOME not set
        FileNotFoundError: If required dirs/files missing

    """
    try:
        _ = os.environ["ANDROID_HOME"]
    except KeyError:
        err = "The ANDROID_HOME environment variable is required."
        raise OSError(err)  # noqa: B904

    if not MODULE_DIR.exists():
        err = f"Module directory does not exist: {MODULE_DIR}"
        raise FileNotFoundError(err)

    if not BUILD_CONFIG_FILE.exists():
        err = f"Build configuration file does not exist: {BUILD_CONFIG_FILE}"
        raise FileNotFoundError(err)

    for entry in (BUILD_DIR, DIST_DIR):
        if not entry.exists():
            entry.mkdir(parents=True)
        elif clear:
            for e in entry.iterdir():
                remove_entry(e)


def load_config(file: Path) -> tuple[PythonConfig, ModuleConfig]:
    """Parse TOML config file and return configurations.

    Args:
        file: Path to TOML file

    Returns:
        Tuple of PythonConfig and ModuleConfig objects

    Raises:
        ValueError: If config is invalid or missing required sections
        Tomllib.TOMLDecodeError: If file cannot be parsed

    """
    with file.open("rb") as fin:
        try:
            config = tomllib.load(fin)
        except tomllib.TOMLDecodeError as e:
            err = f"Failed to parse '{file}': {e}"
            raise ValueError(err) from e

    if "python" not in config or "module" not in config:
        err = f"Configuration file '{file}' must contain 'python' and 'module' sections"
        raise ValueError(err)

    python_config = PythonConfig(**config["python"])
    module_config = ModuleConfig(**config["module"])

    return python_config, module_config


def get_cpython(python_conf: PythonConfig) -> tuple[Path, str]:
    """Get CPython sources.

    Args:
        python_conf: Python build configuration

    Returns:
        Tuple of (source dir path, version string)

    Raises:
        DownloadError: If source download fails
        TarError: If archive extraction fails

    """
    filename = Path(python_conf.sources_url).name

    try:
        version = re.findall(r"(\d+\.\d+\.\d+)", filename)[0]
    except IndexError:
        version = "unknown"

    # Check if sources already exist
    for entry in BUILD_DIR.iterdir():
        if entry.is_dir() and version in entry.name:
            logger.info("Using existing sources: %s", entry)
            return entry, version

    output = BUILD_DIR / filename
    download(python_conf.sources_url, output)

    with tarfile.open(output, "r:gz") as archive:
        archive.extractall(BUILD_DIR, filter="fully_trusted")

    output.unlink()
    return next(BUILD_DIR.iterdir()), version


def build_cpython(sources: Path, python_conf: PythonConfig) -> None:
    """Build CPython for Android.

    Args:
        sources: CPython source directory
        python_conf: Python build configuration

    Raises:
        CalledProcessError: If build commands fail

    """
    android_py = sources / "Android" / "android.py"

    configure_env = os.environ.copy()
    configure_env.update(python_conf.configure_env)

    if not (sources / "cross-build" / "build").exists():
        run(
            [str(android_py), "configure-build", "--", *python_conf.configure_args],
            env=configure_env,
        )
        run([str(android_py), "make-build"])

    for host in python_conf.hosts:
        host_build = sources / "cross-build" / host

        if host_build.exists():
            logger.info("Build for '%s' already exists, skipping build", host)
            continue

        logger.info("Building for host: %s", host)

        run(
            [
                str(android_py),
                "configure-host",
                host,
                "--",
                *python_conf.configure_args,
            ],
            env=configure_env,
        )
        run([str(android_py), "make-host", host])

        if python_conf.package:
            run([str(android_py), "package", host])

            dist = host_build / "dist"
            for entry in dist.iterdir():
                target = DIST_DIR / entry.name
                if target.exists():
                    target.unlink()

                entry.rename(target)


def debloat(
    sources: Path,
    python_conf: PythonConfig,
    module_conf: ModuleConfig,
) -> None:
    """Remove unnecessary files from build.

    Args:
        sources: Root directory of CPython source code
        python_conf: Python build configuration
        module_conf: Module configuration

    """
    cross_build = sources / "cross-build"

    flags = GL | B | E | N

    for host in python_conf.hosts:
        prefix = cross_build / host / "prefix"

        for entry in prefix.glob(module_conf.debloat_patterns, flags=flags):
            try:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                else:
                    rmtree(entry)
            except OSError:
                logger.exception("Failed to remove: %s", entry)
                continue

            logger.info("Removed: %s", entry)


def strip(sources: Path, python_conf: PythonConfig, module_conf: ModuleConfig) -> None:
    """Strip debug info from binaries in prefix/bin for every host.

    Args:
        sources: Root directory of CPython source code
        python_conf: Python build configuration
        module_conf: Module configuration

    """
    android_home = Path(os.environ["ANDROID_HOME"])

    installed_ndks = list((android_home / "ndk").iterdir())
    installed_ndks.sort()

    latest_ndk = installed_ndks[-1]

    strip_bin = (
        latest_ndk
        / "toolchains"
        / "llvm"
        / "prebuilt"
        / "linux-x86_64"
        / "bin"
        / "llvm-strip"
    )

    if not strip_bin.is_file():
        logger.error("Strip binary not found: %s", strip_bin)
        return

    for host in python_conf.hosts:
        prefix = sources / "cross-build" / host / "prefix"

        bindir = prefix / "bin"
        libdir = prefix / "lib"

        walkers = chain(
            bindir.walk(follow_symlinks=False),
            libdir.walk(follow_symlinks=False),
        )

        for path, _, filenames in walkers:
            for file in filenames:
                filepath = path / file

                result = run(
                    [str(strip_bin), *module_conf.strip_args, str(filepath)],
                    log=False,
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    check=False,
                )

                if result.returncode == 0:
                    logger.info("Stripped: %s", filepath)


def replace_shebang(file: Path, pattern: re.Pattern, replacement: str) -> bool:
    """Replace shebang in text file.

    Args:
        file: File to process
        pattern: Regex pattern for shebang
        replacement: New shebang string

    Returns:
        True if shebang was replaced

    """
    try:
        with file.open("rb") as f:
            if is_binary(f.read(1024)):
                return False

            f.seek(0)
            content = f.read().decode()

        if not pattern.match(content):
            return False

        with file.open("w") as f:
            new_content = pattern.sub(replacement, content, 1)
            f.write(new_content)
    except (OSError, UnicodeDecodeError):
        return False
    else:
        return True


def replace_shebangs(
    sources: Path,
    python_conf: PythonConfig,
    module_conf: ModuleConfig,
) -> None:
    """Replace shebangs in bin directory.

    Args:
        sources: Root directory of CPython source code
        python_conf: Python build configuration
        module_conf: Module configuration

    """
    patterns = {
        "python": (
            re.compile(module_conf.shebang_mapping["python"]),
            "#!/system/bin/python3",
        ),
        "shell": (re.compile(module_conf.shebang_mapping["shell"]), "#!/system/bin/sh"),
    }

    for host in python_conf.hosts:
        bindir = sources / "cross-build" / host / "prefix" / "bin"

        for file in (f for f in bindir.iterdir() if f.is_file()):
            for pattern, replacement in patterns.values():
                if replace_shebang(file, pattern, replacement):
                    logger.info("Replaced shebang in: %s", file)
                    break


def main() -> None:
    """Build Magisk modules.

    Steps:
    1. Parse args
    2. Load configs
    3. Get sources
    4. Build Python
    5. Package modules
    """
    parser = ArgumentParser()
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear build and dist directories before building",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__} by {__author__} ({__license__})",
    )
    args = parser.parse_args()

    init(clear=args.clear)

    python_conf, module_conf = load_config(BUILD_CONFIG_FILE)

    logger.info("Getting CPython sources...")
    sources, ver = get_cpython(python_conf)

    if python_conf.apply_patches:
        logger.info("Applying patches...")
        apply_patches(sources)

    logger.info("Building CPython...")
    build_cpython(sources, python_conf)

    if module_conf.debloat:
        logger.info("Debloating...")
        debloat(sources, python_conf, module_conf)
    if module_conf.strip:
        logger.info("Stripping binaries...")
        strip(sources, python_conf, module_conf)
    if module_conf.replace_shebangs:
        logger.info("Replacing shebangs...")
        replace_shebangs(sources, python_conf, module_conf)

    logger.info("Building Magisk modules...")
    ModuleBuilder(
        sources,
        ver,
        python_conf,
        module_conf,
    ).build()


if __name__ == "__main__":
    logging.basicConfig(
        datefmt="%H:%M:%S",
        format="[%(levelname).1s | %(asctime)s] %(message)s",
        level=logging.INFO,
    )

    main()
