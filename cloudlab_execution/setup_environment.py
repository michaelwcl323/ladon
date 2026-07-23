#!/usr/bin/env python3
"""Install and build Ladon on an Ubuntu CloudLab node.

Typical usage:

    python3 cloudlab_execution/setup_environment.py
    source ~/.bashrc

The script:
  1. Installs required Ubuntu packages.
  2. Installs Go 1.21.2 under ~/.local/toolchains.
  3. Creates a GOPATH under ~/ladon-gopath.
  4. Copies Ladon to its required GOPATH import path.
  5. Downloads dependency versions compatible with Go 1.21.
  6. Generates protobuf code and builds the Ladon executables.

It is safe to run the script more than once.
"""

from __future__ import annotations

import argparse
import getpass
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request


GO_VERSION = "1.21.2"
LADON_IMPORT_PATH = Path("github.com/hyperledger-labs/ladon")

APT_PACKAGES = [
    "protobuf-compiler",
    "protobuf-compiler-grpc",
    "git",
    "curl",
    "openssl",
    "jq",
    "graphviz",
    "make",
    "gcc",
    "libc6",
    "python3",
    "python3-numpy",
    "python3-matplotlib",
]

# (GOPATH import path, Git repository, pinned revision)
DEPENDENCIES = [
    ("github.com/c9s/goprocinfo", "https://github.com/c9s/goprocinfo", "c95fcf8"),
    ("github.com/golang/protobuf", "https://github.com/golang/protobuf", "v1.5.3"),
    (
        "github.com/mattn/go-colorable",
        "https://github.com/mattn/go-colorable",
        "v0.1.15",
    ),
    (
        "github.com/mattn/go-isatty",
        "https://github.com/mattn/go-isatty",
        "v0.0.23",
    ),
    ("github.com/op/go-logging", "https://github.com/op/go-logging", "970db52"),
    ("github.com/rs/zerolog", "https://github.com/rs/zerolog", "v1.29.1"),
    ("go.dedis.ch/fixbuf", "https://github.com/dedis/fixbuf", "6f51ba7"),
    ("go.dedis.ch/kyber", "https://github.com/dedis/kyber", "v3.1.0"),
    ("golang.org/x/crypto", "https://go.googlesource.com/crypto", "v0.12.0"),
    ("golang.org/x/net", "https://go.googlesource.com/net", "v0.9.0"),
    ("golang.org/x/sys", "https://go.googlesource.com/sys", "v0.7.0"),
    ("golang.org/x/text", "https://go.googlesource.com/text", "v0.9.0"),
    (
        "google.golang.org/genproto",
        "https://github.com/googleapis/go-genproto",
        "0005af68ea54",
    ),
    ("google.golang.org/grpc", "https://github.com/grpc/grpc-go", "v1.57.0"),
    (
        "google.golang.org/protobuf",
        "https://go.googlesource.com/protobuf",
        "v1.30.0",
    ),
    ("gopkg.in/yaml.v2", "https://gopkg.in/yaml.v2", "v2.4.0"),
]

BASHRC_START = "# >>> Ladon CloudLab environment >>>"
BASHRC_END = "# <<< Ladon CloudLab environment <<<"


def message(text: str) -> None:
    print(f"\n[ladon-setup] {text}", flush=True)


def execute(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> None:
    print("+ " + " ".join(command), flush=True)
    if not dry_run:
        subprocess.run(command, cwd=cwd, env=env, check=True)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Ladon source checkout (default: repository containing this script)",
    )
    parser.add_argument(
        "--gopath",
        type=Path,
        default=Path.home() / "ladon-gopath",
        help="GOPATH to create (default: ~/ladon-gopath)",
    )
    parser.add_argument(
        "--skip-system-packages",
        action="store_true",
        help="do not run sudo apt-get",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="prepare the environment without compiling Ladon",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print intended operations without changing the node",
    )
    return parser.parse_args()


def go_architecture() -> str:
    architecture = platform.machine().lower()
    supported = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    try:
        return supported[architecture]
    except KeyError as error:
        raise RuntimeError(
            f"Unsupported CPU architecture: {architecture}. "
            "Expected x86_64 or aarch64."
        ) from error


def install_apt_packages(dry_run: bool) -> None:
    if shutil.which("apt-get") is None:
        raise RuntimeError(
            "apt-get is unavailable. Install the required packages manually "
            "and rerun with --skip-system-packages."
        )
    execute(["sudo", "apt-get", "update"], dry_run=dry_run)
    execute(
        ["sudo", "apt-get", "install", "-y", *APT_PACKAGES],
        dry_run=dry_run,
    )


def find_official_go_download(architecture: str) -> tuple[str, str]:
    filename = f"go{GO_VERSION}.linux-{architecture}.tar.gz"
    metadata_url = "https://go.dev/dl/?mode=json&include=all"

    with urllib.request.urlopen(metadata_url, timeout=30) as response:
        releases = json.load(response)

    for release in releases:
        if release.get("version") != f"go{GO_VERSION}":
            continue
        for artifact in release.get("files", []):
            if artifact.get("filename") == filename:
                return f"https://go.dev/dl/{filename}", artifact["sha256"]

    raise RuntimeError(f"The Go download service did not list {filename}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safely_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError(f"Unsafe path in Go archive: {member.name}")
        # No filter= argument: CloudLab Ubuntu 22.04 commonly uses Python 3.10.
        tar.extractall(destination)


def install_go(toolchain: Path, dry_run: bool) -> None:
    go_binary = toolchain / "bin/go"
    if go_binary.exists():
        # A previous Ladon installer may have exported GOROOT=$HOME/go.
        # An explicitly selected Go binary still reads inherited GOROOT, so
        # override it while validating this toolchain.
        check_environment = os.environ.copy()
        check_environment["GOROOT"] = str(toolchain)
        check_environment["PATH"] = (
            f"{toolchain / 'bin'}:{check_environment.get('PATH', '')}"
        )
        version = subprocess.check_output(
            [str(go_binary), "version"],
            text=True,
            env=check_environment,
        )
        if f"go{GO_VERSION}" not in version:
            raise RuntimeError(
                f"{toolchain} contains an unexpected Go version: {version.strip()}"
            )
        message(f"Go {GO_VERSION} is already installed")
        return

    if dry_run:
        message(f"Would install Go {GO_VERSION} at {toolchain}")
        return

    url, expected_hash = find_official_go_download(go_architecture())
    toolchain.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ladon-go-") as temporary_name:
        temporary = Path(temporary_name)
        archive = temporary / Path(url).name

        message(f"Downloading {url}")
        urllib.request.urlretrieve(url, archive)

        actual_hash = file_sha256(archive)
        if actual_hash != expected_hash:
            raise RuntimeError(
                "Go archive checksum mismatch: "
                f"expected {expected_hash}, got {actual_hash}"
            )

        safely_extract(archive, temporary)
        shutil.move(str(temporary / "go"), toolchain)


def build_environment(gopath: Path, toolchain: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["GOROOT"] = str(toolchain)
    environment["GOPATH"] = str(gopath)
    environment["GOCACHE"] = str(Path.home() / ".cache/ladon-go-build")
    environment["GO111MODULE"] = "off"
    environment["PATH"] = (
        f"{toolchain / 'bin'}:{gopath / 'bin'}:"
        f"{environment.get('PATH', '')}"
    )
    return environment


def configure_bashrc(gopath: Path, toolchain: Path, dry_run: bool) -> None:
    bashrc = Path.home() / ".bashrc"
    configuration = "\n".join(
        [
            BASHRC_START,
            f'export GOROOT="{toolchain}"',
            f'export GOPATH="{gopath}"',
            'export GOCACHE="$HOME/.cache/ladon-go-build"',
            "export GO111MODULE=off",
            'export PATH="$GOROOT/bin:$GOPATH/bin:$PATH"',
            BASHRC_END,
        ]
    )

    current = bashrc.read_text() if bashrc.exists() else ""
    old_block = re.compile(
        rf"\n?{re.escape(BASHRC_START)}.*?{re.escape(BASHRC_END)}\n?",
        re.DOTALL,
    )
    updated = old_block.sub("\n", current).rstrip()
    updated = updated + "\n\n" + configuration + "\n"

    if dry_run:
        message(f"Would update {bashrc}")
    else:
        bashrc.write_text(updated)


def copy_ladon(source: Path, destination: Path, dry_run: bool) -> None:
    source = source.resolve()
    if not (source / "deployment/deploy.sh").is_file():
        raise RuntimeError(f"{source} does not appear to be a Ladon checkout")

    if source == destination.resolve(strict=False):
        message("Ladon is already located inside the selected GOPATH")
        return

    if dry_run:
        message(f"Would copy {source} to {destination}")
        return

    ignored = {".git", ".local", "__pycache__", "deployment-data"}

    def ignore_names(_directory: str, names: list[str]) -> set[str]:
        return ignored.intersection(names)

    message(f"Copying Ladon to {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=ignore_names,
    )


def configure_deployment_user(repository: Path, dry_run: bool) -> None:
    username = getpass.getuser()
    groupname = grp.getgrgid(pwd.getpwnam(username).pw_gid).gr_name
    vars_file = repository / "deployment/vars.sh"

    if dry_run:
        message(f"Would set deployment user to {username}:{groupname}")
        return

    contents = vars_file.read_text()
    contents = re.sub(
        r'^user=".*"$',
        f'user="{username}"',
        contents,
        flags=re.MULTILINE,
    )
    contents = re.sub(
        r'^group=".*"$',
        f'group="{groupname}"',
        contents,
        flags=re.MULTILINE,
    )
    vars_file.write_text(contents)


def install_dependencies(gopath: Path, dry_run: bool) -> None:
    source_root = gopath / "src"

    for import_path, repository_url, revision in DEPENDENCIES:
        destination = source_root / import_path
        exists = (destination / ".git").is_dir()

        if not exists:
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
            execute(
                ["git", "clone", repository_url, str(destination)],
                dry_run=dry_run,
            )
        else:
            execute(
                ["git", "-C", str(destination), "fetch", "--tags", "origin"],
                dry_run=dry_run,
            )

        execute(
            [
                "git",
                "-C",
                str(destination),
                "checkout",
                "--detach",
                revision,
            ],
            dry_run=dry_run,
        )


def build_ladon(
    repository: Path,
    environment: dict[str, str],
    dry_run: bool,
) -> None:
    if not dry_run:
        Path(environment["GOCACHE"]).mkdir(parents=True, exist_ok=True)

    execute(
        ["go", "install", "github.com/golang/protobuf/protoc-gen-go"],
        env=environment,
        dry_run=dry_run,
    )
    execute(
        ["./run-protoc.sh"],
        cwd=repository,
        env=environment,
        dry_run=dry_run,
    )
    execute(
        ["go", "install", "github.com/hyperledger-labs/ladon/cmd/..."],
        cwd=repository,
        env=environment,
        dry_run=dry_run,
    )

    if dry_run:
        return

    required_binaries = [
        "discoverymaster",
        "discoveryslave",
        "orderingpeer",
        "orderingclient",
    ]
    binary_directory = Path(environment["GOPATH"]) / "bin"
    missing = [
        name for name in required_binaries
        if not (binary_directory / name).is_file()
    ]
    if missing:
        raise RuntimeError(
            "Build completed without expected binaries: " + ", ".join(missing)
        )


def main() -> int:
    options = arguments()
    source = options.source.expanduser().resolve()
    gopath = options.gopath.expanduser().resolve()
    toolchain = Path.home() / ".local/toolchains" / f"go{GO_VERSION}"
    repository = gopath / "src" / LADON_IMPORT_PATH

    try:
        if not options.skip_system_packages:
            message("Installing Ubuntu system packages")
            install_apt_packages(options.dry_run)

        install_go(toolchain, options.dry_run)
        environment = build_environment(gopath, toolchain)
        configure_bashrc(gopath, toolchain, options.dry_run)
        copy_ladon(source, repository, options.dry_run)
        configure_deployment_user(repository, options.dry_run)

        message("Installing pinned Go dependencies")
        install_dependencies(gopath, options.dry_run)

        if not options.skip_build:
            message("Generating protobuf code and building Ladon")
            build_ladon(repository, environment, options.dry_run)

    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as error:
        print(f"\n[ladon-setup] ERROR: {error}", file=sys.stderr)
        return 1

    message("Environment setup completed successfully")
    print(f"Repository: {repository}")
    print(f"GOPATH:     {gopath}")
    print(f"Binaries:   {gopath / 'bin'}")
    print("\nLoad the environment:")
    print("  source ~/.bashrc")
    print("\nRun a local experiment:")
    print(f"  cd {repository / 'deployment'}")
    print(
        "  ./deploy.sh local new "
        "scripts/experiment-configuration/generate-local-config.sh"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
