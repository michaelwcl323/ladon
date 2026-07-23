#!/usr/bin/env python3
"""Run Ladon experiments.

Edit the parameters at the beginning of ``local()``, then run:

    python3 cloudlab_execution/run_experiment.py --local
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


LADON_IMPORT_PATH = Path("src/github.com/hyperledger-labs/ladon")
LOCAL_GENERATOR = Path(
    "deployment/scripts/experiment-configuration/generate-local-config.sh"
)
CLOUDLAB_SETTINGS = Path(__file__).resolve().parent / "cloudlab_settings.json"


@dataclass(frozen=True)
class LocalConfig:
    peers: int
    failures: int
    stragglers: int
    clients: int
    duration: int
    throughput: int
    orderer: str
    batch_size: int
    segment_length: int
    view_change_timeout: int
    leader_policy: str
    authentication: bool
    use_signatures: bool
    fix_batch_rate: bool


@dataclass(frozen=True)
class CloudLabHost:
    hostname: str
    username: str
    port: int
    region: str
    private_hostname: str


def local(*, dry_run: bool = False) -> None:
    """Configure and execute one local experiment.

    Change the values in this block. All peers, clients, and the discovery
    master will run on the current machine.
    """

    # ================================================================
    # EDIT LOCAL EXPERIMENT PARAMETERS HERE
    # ================================================================
    config = LocalConfig(
        peers=4,                   # Number of non-faulty peers
        failures=0,                # Additional faulty peers
        stragglers=0,              # Slow peers; supported with Pbft
        clients=4,                 # Client processes
        duration=60,               # Experiment duration, seconds
        throughput=20000,          # Target throughput, requests/second
        orderer="Pbft",            # Pbft, HotStuff, Raft, or Dummy
        batch_size=4096,           # Maximum requests per batch
        segment_length=32,         # Entries per segment
        view_change_timeout=60000, # Milliseconds
        leader_policy="Simple",    # Simple, Single, Backoff, etc.
        authentication=True,       # Authenticate client requests
        use_signatures=False,      # Use protocol signatures
        fix_batch_rate=True,       # Use a fixed batch rate
    )
    # ================================================================
    # END OF PARAMETERS
    # ================================================================

    validate_local_config(config)
    repository, gopath = locate_ladon()
    deployment_directory = repository / "deployment"
    base_generator = repository / LOCAL_GENERATOR
    generated_config = create_local_generator(base_generator.read_text(), config)

    environment = os.environ.copy()
    environment["GOPATH"] = str(gopath)
    environment["GO111MODULE"] = "off"
    environment["PATH"] = f"{gopath / 'bin'}:{environment.get('PATH', '')}"

    generated_directory = deployment_directory / ".generated-configs"
    generated_name = "local-config.generated.sh"
    generated_path = generated_directory / generated_name
    relative_generated_path = generated_path.relative_to(deployment_directory)
    command = [
        "./deploy.sh",
        "local",
        "new",
        str(relative_generated_path),
    ]

    print_local_summary(config, repository, command)
    if dry_run:
        print("\nDry run: the experiment was not started.")
        return

    generated_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=generated_directory,
        prefix="local-config-",
        suffix=".sh",
        delete=False,
    ) as temporary:
        temporary.write(generated_config)
        actual_generated_path = Path(temporary.name)

    actual_generated_path.chmod(0o755)
    actual_relative_path = actual_generated_path.relative_to(deployment_directory)
    command[-1] = str(actual_relative_path)

    try:
        result_directory = run_deployment(
            command,
            deployment_directory,
            environment,
        )
        print_result_summary(result_directory)
    finally:
        actual_generated_path.unlink(missing_ok=True)


def remote(*, dry_run: bool = False) -> None:
    """Run an experiment on hosts from cloudlab_settings.json."""

    # ================================================================
    # EDIT REMOTE EXPERIMENT PARAMETERS HERE
    # The controller below runs both master and client. Every entry in
    # cloudlab_settings.json is used as a consensus peer.
    # ================================================================
    controller_hostname = "10.10.1.11"
    controller_private_hostname = "10.10.1.11"

    config = LocalConfig(
        peers=10,
        failures=0,
        stragglers=0,
        clients=8,                 # Client processes on 10.10.1.11
        duration=60,
        throughput=20000,
        orderer="Pbft",
        batch_size=4096,
        segment_length=16,
        view_change_timeout=60000,
        leader_policy="Simple",
        authentication=True,
        use_signatures=False,
        fix_batch_rate=True,
    )
    # ================================================================

    validate_local_config(config)
    settings = load_cloudlab_settings()
    key_path = Path(settings["key"]["path"]).expanduser().resolve()
    hosts = parse_cloudlab_hosts(settings)
    if len(hosts) < config.peers:
        raise ValueError(
            f"Remote experiment needs {config.peers} peer hosts, "
            f"but settings contains {len(hosts)}"
        )
    peers = hosts[:config.peers]
    template_host = peers[0]
    master = CloudLabHost(
        hostname=controller_hostname,
        username=template_host.username,
        port=template_host.port,
        region=template_host.region,
        private_hostname=controller_private_hostname,
    )
    client = master
    selected = [master, *peers]
    ensure_uniform_ssh(selected)
    remote_home = preflight_remote_hosts(selected, key_path, dry_run=dry_run)

    repository = Path(__file__).resolve().parents[1]
    deployment_directory = repository / "deployment"
    base_generator = repository / LOCAL_GENERATOR
    generated_config = create_local_generator(base_generator.read_text(), config)

    print_remote_summary(config, key_path, master, peers, client)
    if dry_run:
        print("\nDry run: SSH was checked, but the experiment was not started.")
        return

    generated_directory = deployment_directory / ".generated-configs"
    generated_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=generated_directory,
        prefix="remote-config-",
        suffix=".sh",
        delete=False,
    ) as config_file:
        config_file.write(generated_config)
        config_path = Path(config_file.name)
    config_path.chmod(0o755)

    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=generated_directory,
        prefix="cloudlab-instances-",
        suffix=".txt",
        delete=False,
    ) as topology_file:
        topology_path = Path(topology_file.name)
        topology_file.write(instance_line("master", master, "master"))
        for number, host in enumerate(peers, start=1):
            topology_file.write(instance_line(f"p{number}", host, "peers"))
        topology_file.write(instance_line("client", client, "1client"))

    environment = os.environ.copy()
    environment.update(
        {
            "LADON_SSH_KEY_FILE": str(key_path),
            "LADON_SSH_PORT": str(master.port),
            "LADON_MASTER_PORT": str(settings.get("port", 9999)),
            "LADON_REMOTE_USER": master.username,
            "LADON_REMOTE_HOME": remote_home,
            "LADON_REMOTE_GOPATH": f"{remote_home}/ladon-gopath",
            "LADON_REMOTE_GOROOT": (
                f"{remote_home}/.local/toolchains/go1.21.2"
            ),
        }
    )
    command = [
        "./deploy.sh",
        "remote",
        str(topology_path),
        "new",
        str(config_path),
    ]
    print("\nCommand:")
    print("  " + " ".join(shlex.quote(part) for part in command))

    try:
        subprocess.run(
            command,
            cwd=deployment_directory,
            env=environment,
            check=True,
        )
    finally:
        config_path.unlink(missing_ok=True)
        topology_path.unlink(missing_ok=True)


def load_cloudlab_settings() -> dict:
    if not CLOUDLAB_SETTINGS.is_file():
        raise FileNotFoundError(
            f"CloudLab settings not found: {CLOUDLAB_SETTINGS}"
        )
    with CLOUDLAB_SETTINGS.open() as source:
        settings = json.load(source)
    if "key" not in settings or "path" not in settings["key"]:
        raise ValueError("cloudlab_settings.json is missing key.path")
    if not isinstance(settings.get("hosts"), list) or not settings["hosts"]:
        raise ValueError("cloudlab_settings.json must contain a non-empty hosts list")
    key_path = Path(settings["key"]["path"]).expanduser()
    if not key_path.is_file():
        raise FileNotFoundError(f"SSH private key not found: {key_path}")
    return settings


def parse_cloudlab_hosts(settings: dict) -> list[CloudLabHost]:
    result = []
    for index, raw in enumerate(settings["hosts"]):
        try:
            hostname = str(raw["hostname"])
            username = str(raw["username"])
        except KeyError as error:
            raise ValueError(f"hosts[{index}] is missing {error.args[0]}") from error
        result.append(
            CloudLabHost(
                hostname=hostname,
                username=username,
                port=int(raw.get("port", 22)),
                region=str(raw.get("region", "cloudlab")),
                private_hostname=str(raw.get("private_hostname", hostname)),
            )
        )
    return result


def ensure_uniform_ssh(hosts: list[CloudLabHost]) -> None:
    users = {host.username for host in hosts}
    ports = {host.port for host in hosts}
    if len(users) != 1:
        raise ValueError("All selected hosts must use the same SSH username")
    if len(ports) != 1:
        raise ValueError("All selected hosts must use the same SSH port")


def ssh_command(key_path: Path, host: CloudLabHost, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-A",
        "-i",
        str(key_path),
        "-p",
        str(host.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{host.username}@{host.hostname}",
        remote_command,
    ]


def preflight_remote_hosts(
    hosts: list[CloudLabHost],
    key_path: Path,
    *,
    dry_run: bool,
) -> str:
    print("\nChecking CloudLab hosts...")
    homes = set()
    for index, host in enumerate(hosts):
        command = ssh_command(
            key_path,
            host,
            "printf '%s\\n' \"$HOME\"; "
            "test -x \"$HOME/.local/toolchains/go1.21.2/bin/go\" "
            "&& echo go-ok; "
            "command -v protoc >/dev/null && echo protoc-ok; "
            "command -v rsync >/dev/null && echo rsync-ok",
        )
        print(f"  [{index}] {host.username}@{host.hostname}:{host.port}", end="")
        if dry_run:
            # A dry run still checks connectivity and required tools.
            pass
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"SSH/environment check failed for {host.hostname}: {detail}"
            )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) < 4:
            raise RuntimeError(
                f"{host.hostname} is missing go, protoc, or rsync. Run "
                "setup_environment.py on that host first."
            )
        homes.add(lines[0])
        print("  OK")
    if len(homes) != 1:
        raise ValueError(f"Selected hosts have different home directories: {homes}")
    return homes.pop()


def instance_line(identifier: str, host: CloudLabHost, tag: str) -> str:
    return (
        f"{identifier} {host.hostname} {host.private_hostname} "
        f"{tag} {host.region}\n"
    )


def print_remote_summary(
    config: LocalConfig,
    key_path: Path,
    master: CloudLabHost,
    peers: list[CloudLabHost],
    client: CloudLabHost,
) -> None:
    print("\nLadon remote experiment")
    print(f"  Settings:          {CLOUDLAB_SETTINGS}")
    print(f"  SSH key:           {key_path}")
    print(f"  Master:            {master.hostname}")
    print(f"  Peers:             {len(peers)}")
    for index, host in enumerate(peers):
        print(f"    peer {index:<2}         {host.private_hostname}")
    print(f"  Client:            {client.hostname}")
    print(f"  Duration:          {config.duration} seconds")
    print(f"  Target throughput: {config.throughput} req/s")
    print(f"  Protocol:          {config.orderer}")


def validate_local_config(config: LocalConfig) -> None:
    positive_values = {
        "peers": config.peers,
        "clients": config.clients,
        "duration": config.duration,
        "throughput": config.throughput,
        "batch_size": config.batch_size,
        "segment_length": config.segment_length,
        "view_change_timeout": config.view_change_timeout,
    }
    for name, value in positive_values.items():
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")

    if config.failures < 0 or config.stragglers < 0:
        raise ValueError("failures and stragglers cannot be negative")
    if config.stragglers > config.peers + config.failures:
        raise ValueError("stragglers cannot exceed the total peer count")

    valid_orderers = {"Pbft", "HotStuff", "Raft", "Dummy"}
    if config.orderer not in valid_orderers:
        raise ValueError(
            f"orderer must be one of: {', '.join(sorted(valid_orderers))}"
        )
    if config.stragglers and config.orderer != "Pbft":
        raise ValueError("stragglers are supported only with orderer='Pbft'")

    valid_policies = {"Simple", "Single", "Backoff", "Blacklist", "Combined"}
    if config.leader_policy not in valid_policies:
        raise ValueError(
            "leader_policy must be one of: "
            + ", ".join(sorted(valid_policies))
        )


def locate_ladon() -> tuple[Path, Path]:
    """Locate the GOPATH copy produced by setup_environment.py."""

    gopath_value = os.environ.get("GOPATH")
    gopath = (
        Path(gopath_value).expanduser()
        if gopath_value
        else Path.home() / "ladon-gopath"
    ).resolve()
    repository = gopath / LADON_IMPORT_PATH

    if not (repository / LOCAL_GENERATOR).is_file():
        raise FileNotFoundError(
            f"Ladon was not found at {repository}. Run "
            "'python3 cloudlab_execution/setup_environment.py' first, "
            "then run 'source ~/.bashrc'."
        )
    return repository, gopath


def replace_scalar(text: str, name: str, value: object) -> str:
    pattern = re.compile(rf"^{re.escape(name)}=.*$", re.MULTILINE)
    updated, count = pattern.subn(f'{name}="{value}"', text, count=1)
    if count != 1:
        raise RuntimeError(f"Missing setting in generator: {name}")
    return updated


def replace_array(text: str, name: str, value: object) -> str:
    pattern = re.compile(rf"^{re.escape(name)}=\([^)]*\).*$", re.MULTILINE)
    updated, count = pattern.subn(f"{name}=({value})", text, count=1)
    if count != 1:
        raise RuntimeError(f"Missing array setting in generator: {name}")
    return updated


def boolean(value: bool) -> str:
    return str(value).lower()


def replace_throughput(text: str, config: LocalConfig) -> str:
    authentication = "Auth" if config.authentication else "NoAuth"
    single = "Single" if config.leader_policy == "Single" else ""
    variable = f"throughputs{authentication}{single}{config.orderer}"
    setting = f"{variable}[{config.peers}]"
    pattern = re.compile(rf"^{re.escape(setting)}=.*$", re.MULTILINE)
    updated, count = pattern.subn(
        f'{setting}="{config.throughput}"',
        text,
        count=1,
    )
    if count == 0:
        # The original scripts predeclare only 4/8/16/32/64/128 peers.
        # CloudLab topologies may use other sizes, such as 10 peers.
        declaration = re.compile(
            rf"^({re.escape(variable)}=.*)$",
            re.MULTILINE,
        )
        updated, declaration_count = declaration.subn(
            rf'\1\n{setting}="{config.throughput}"',
            text,
            count=1,
        )
        if declaration_count != 1:
            raise RuntimeError(
                f"The base generator does not define throughput array {variable}"
            )
    return updated


def create_local_generator(text: str, config: LocalConfig) -> str:
    scalar_settings = {
        "clients1": config.clients,
        "clients16": "",
        "clients32": "",
        "systemSizes": config.peers,
        "durations": config.duration,
        "orderers": config.orderer,
        "batchsizes": config.batch_size,
        "segmentLengths": config.segment_length,
        "viewChangeTimeouts": config.view_change_timeout,
        "leaderPolicies": config.leader_policy,
        "auths": boolean(config.authentication),
        "fixBatchRate": boolean(config.fix_batch_rate),
        "crashTimings": "Straggler" if config.stragglers else "EpochEnd",
    }
    for name, value in scalar_settings.items():
        text = replace_scalar(text, name, value)

    text = replace_array(text, "failureCounts", config.failures)
    text = replace_array(text, "StragglerCnt", config.stragglers)
    text = replace_array(text, "UseSig", boolean(config.use_signatures))
    return replace_throughput(text, config)


def print_local_summary(
    config: LocalConfig,
    repository: Path,
    command: list[str],
) -> None:
    print("\nLadon local experiment")
    print(f"  Repository:          {repository}")
    print(f"  Correct peers:       {config.peers}")
    print(f"  Faulty peers:        {config.failures}")
    print(f"  Stragglers:          {config.stragglers}")
    print(f"  Clients:             {config.clients}")
    print(f"  Duration:            {config.duration} seconds")
    print(f"  Target throughput:   {config.throughput} req/s")
    print(f"  Orderer:             {config.orderer}")
    print(f"  Batch size:          {config.batch_size}")
    print(f"  Segment length:      {config.segment_length}")
    print(f"  Leader policy:       {config.leader_policy}")
    print(f"  Authentication:      {config.authentication}")
    print(f"  Protocol signatures: {config.use_signatures}")
    print("\nCommand:")
    print("  " + " ".join(shlex.quote(part) for part in command))


def run_deployment(
    command: list[str],
    deployment_directory: Path,
    environment: dict[str, str],
) -> Path:
    """Run deploy.sh while hiding its two unreadable wide CSV lines."""

    process = subprocess.Popen(
        command,
        cwd=deployment_directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("Could not capture deployment output")

    result_directory: Path | None = None
    summary_phase = False

    for line in process.stdout:
        stripped = line.rstrip("\n")
        if stripped == "Generating result summary.":
            summary_phase = True
            print(stripped, flush=True)
            continue

        # summarize.sh prints a 77-column header and row. Preserve them in
        # result-summary.csv, but do not flood the terminal with those lines.
        if summary_phase and stripped.count(",") > 20:
            continue

        print(stripped, flush=True)
        marker = "Done. Experiment data directory:"
        if stripped.startswith(marker):
            raw_path = stripped[len(marker):].strip()
            candidate = Path(raw_path)
            result_directory = (
                candidate
                if candidate.is_absolute()
                else deployment_directory / candidate
            )

    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)
    if result_directory is None:
        raise RuntimeError("Deployment completed without reporting a result directory")
    return result_directory.resolve()


def display_value(
    row: dict[str, str],
    key: str,
    *,
    unit: str = "",
    decimals: int = 2,
) -> str:
    raw = row.get(key, "").strip()
    if raw in {"", "None", "null", "NULL"}:
        return "N/A"
    try:
        number = float(raw)
    except ValueError:
        return raw
    value = f"{number:.{decimals}f}"
    return f"{value} {unit}".rstrip()


def print_result_summary(result_directory: Path) -> None:
    summary_file = result_directory / "result-summary.csv"
    if not summary_file.is_file():
        print(f"\nResult summary was not found: {summary_file}")
        return

    with summary_file.open(newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        print(f"\nResult summary contains no experiment rows: {summary_file}")
        return

    for row in rows:
        target_raw = row.get("target-throughput", "")
        actual_raw = row.get("throughput-raw", "")
        achievement = "N/A"
        try:
            target = float(target_raw)
            actual = float(actual_raw)
            if target:
                achievement = f"{actual / target * 100:.1f}%"
        except (TypeError, ValueError):
            pass

        truncated_available = (
            row.get("nreq-trunc", "").strip() not in {"", "0", "None"}
        )
        metrics = [
            ("Experiment", row.get("exp", "N/A")),
            ("Topology", f"{row.get('peers', '?')} peers, "
                         f"{row.get('clients', '?')} client machine(s)"),
            ("Protocol", f"{row.get('orderer', 'N/A')} / "
                         f"{row.get('leader-policy', 'N/A')} leaders"),
            ("Duration", display_value(row, "duration-raw", unit="s")),
            ("Target throughput", display_value(
                row, "target-throughput", unit="req/s", decimals=0
            )),
            ("Actual throughput", display_value(
                row, "throughput-raw", unit="req/s"
            )),
            ("Target achieved", achievement),
            ("Average latency", display_value(
                row, "latency-avg-raw", unit="ms"
            )),
            ("P95 latency", display_value(
                row, "latency-95pctile-raw", unit="ms"
            )),
            ("Latency stddev", display_value(
                row, "latency-stdev-raw", unit="ms"
            )),
            ("Sampled requests", display_value(
                row, "nreq-raw", decimals=0
            )),
            ("Proposal rate", display_value(
                row, "propose-rate-raw", unit="batch/s"
            )),
            ("Epochs min/avg/max",
             f"{display_value(row, 'epochs-min')} / "
             f"{display_value(row, 'epochs-avg')} / "
             f"{display_value(row, 'epochs-max')}"),
            ("View changes", display_value(
                row, "viewchanges-total", decimals=0
            )),
            ("Stable-window data", "available" if truncated_available else "N/A"),
        ]

        label_width = max(len(label) for label, _ in metrics)
        value_width = max(len(value) for _, value in metrics)
        border = f"+-{'-' * label_width}-+-{'-' * value_width}-+"

        print("\nExperiment result")
        print(border)
        for label, value in metrics:
            print(f"| {label:<{label_width}} | {value:<{value_width}} |")
        print(border)

    print(f"\nFull CSV: {summary_file}")
    if any(
        row.get("nreq-trunc", "").strip() in {"", "0", "None"}
        for row in rows
    ):
        print(
            "Note: stable-window metrics are unavailable. Use duration >= 30 "
            "seconds because the analyzer removes 5 seconds at each end."
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Ladon experiment.")
    parser.add_argument(
        "--local",
        action="store_true",
        help="run the configuration defined inside local()",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="run remote() using cloudlab_settings.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and display the local configuration without executing",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.local == arguments.remote:
        print("Choose exactly one mode: --local or --remote", file=sys.stderr)
        return 2

    try:
        if arguments.local:
            local(dry_run=arguments.dry_run)
        else:
            remote(dry_run=arguments.dry_run)
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"\n[ladon-runner] ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
