#!/usr/bin/env python3
"""Run a CloudLab experiment matrix over input rates and orderers.

Edit ``INPUT_RATES``, ``ORDERERS``, and the shared experiment parameters
below, or override the two lists on the command line:

    python3 cloudlab_execution/run_experiment_matrix.py \
        --input-rates 20000 40000 60000 \
        --orderers Pbft HotStuff \
        --runs 2

Every input-rate/orderer pair is repeated ``runs`` times. Each run is an
independent experiment with its own result directory and text summary.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from itertools import product
from pathlib import Path
import subprocess
import sys

import run_experiment as runner
import summarize_matrix_results as summarizer


# =====================================================================
# EDIT THE EXPERIMENT MATRIX HERE
# The full Cartesian product is run in order. For example, three rates
# and two orderers produce six experiments.
# =====================================================================
INPUT_RATES = [20000, 40000, 60000, 80000]  # requests/second
ORDERERS = ["Pbft", "HotStuff"]  # Pbft, HotStuff, Raft, or Dummy
RUNS = 2             # Repeat every input-rate/orderer combination

CONTROLLER_HOSTNAME = "10.10.1.11"
CONTROLLER_PRIVATE_HOSTNAME = "10.10.1.11"

PEERS = 10
FAILURES = 0
STRAGGLERS = 0
CLIENTS = 8
DURATION = 60
BATCH_SIZE = 4096
PAYLOAD_SIZE = 500           # Request payload, bytes
SEGMENT_LENGTH = 16
VIEW_CHANGE_TIMEOUT = 60000
LEADER_POLICY = "Simple"
AUTHENTICATION = True
USE_SIGNATURES = False
FIX_BATCH_RATE = True
# =====================================================================


VALID_ORDERERS = ("Pbft", "HotStuff", "Raft", "Dummy")
RESULT_ROOT = Path(__file__).resolve().parent / "result"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-rates",
        nargs="+",
        type=int,
        default=INPUT_RATES,
        metavar="REQ_PER_SEC",
        help="total input-rate list in requests/second",
    )
    parser.add_argument(
        "--orderers",
        nargs="+",
        choices=VALID_ORDERERS,
        default=ORDERERS,
        metavar="ORDERER",
        help="orderer list; choices: %(choices)s",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=RUNS,
        metavar="COUNT",
        help="number of times to repeat every input-rate/orderer combination",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check SSH/environment and display each run without starting it",
    )
    return parser.parse_args()


def unique(values: list[int] | list[str]) -> list[int] | list[str]:
    """Remove duplicate list entries while preserving their order."""

    return list(dict.fromkeys(values))


def experiment_config(input_rate: int, orderer: str) -> runner.LocalConfig:
    return runner.LocalConfig(
        peers=PEERS,
        failures=FAILURES,
        stragglers=STRAGGLERS,
        clients=CLIENTS,
        duration=DURATION,
        throughput=input_rate,
        orderer=orderer,
        batch_size=BATCH_SIZE,
        payload_size=PAYLOAD_SIZE,
        segment_length=SEGMENT_LENGTH,
        view_change_timeout=VIEW_CHANGE_TIMEOUT,
        leader_policy=LEADER_POLICY,
        authentication=AUTHENTICATION,
        use_signatures=USE_SIGNATURES,
        fix_batch_rate=FIX_BATCH_RATE,
    )


def prepare_result_directories(
    orderers: list[str],
    *,
    result_root: Path = RESULT_ROOT,
) -> None:
    """Create result/Ladon-{Orderer} directories before experiments start."""

    for orderer in orderers:
        (result_root / f"Ladon-{orderer}").mkdir(parents=True, exist_ok=True)


def write_result_file(
    result_directory: Path,
    config: runner.LocalConfig,
    *,
    run_number: int,
    total_runs: int,
    result_root: Path = RESULT_ROOT,
    timestamp: datetime | None = None,
) -> Path:
    """Write one matrix result under cloudlab_execution/result."""

    rows = runner.load_result_summary(result_directory)
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected one experiment in {result_directory}, found {len(rows)}"
        )

    orderer_directory = result_root / f"Ladon-{config.orderer}"
    orderer_directory.mkdir(parents=True, exist_ok=True)
    actual_nodes = config.peers + config.failures
    time_text = (timestamp or datetime.now()).strftime("%Y%m%d-%H%M%S")
    output_file = orderer_directory / (
        f"{actual_nodes}-{config.throughput}-run{run_number:02d}-"
        f"{time_text}.txt"
    )
    parameters = [
        ("Run", f"{run_number}/{total_runs}"),
        ("Controller", CONTROLLER_HOSTNAME),
        ("Controller private", CONTROLLER_PRIVATE_HOSTNAME),
        ("Correct peers", str(config.peers)),
        ("Faulty peers", str(config.failures)),
        ("Stragglers", str(config.stragglers)),
        ("Total nodes", str(actual_nodes)),
        ("Client processes", str(config.clients)),
        ("Configured duration", f"{config.duration} s"),
        ("Input rate", f"{config.throughput} req/s"),
        ("Orderer", config.orderer),
        ("Batch size", f"{config.batch_size} requests"),
        ("Payload size", f"{config.payload_size} bytes"),
        ("Segment length", f"{config.segment_length} entries"),
        ("View-change timeout", f"{config.view_change_timeout} ms"),
        ("Leader policy", config.leader_policy),
        (
            "Authentication",
            "enabled" if config.authentication else "disabled",
        ),
        (
            "Protocol signatures",
            "enabled" if config.use_signatures else "disabled",
        ),
        (
            "Fixed batch rate",
            "enabled" if config.fix_batch_rate else "disabled",
        ),
    ]
    output_file.write_text(
        runner.format_result_summary_row(
            rows[0],
            parameters=parameters,
        ) + "\n",
        encoding="utf-8",
    )
    return output_file


def main() -> int:
    arguments = parse_arguments()
    if arguments.runs <= 0:
        print(
            "[ladon-matrix] ERROR: --runs must be greater than zero",
            file=sys.stderr,
        )
        return 2

    input_rates = unique(arguments.input_rates)
    orderers = unique(arguments.orderers)
    if any(rate <= 0 for rate in input_rates):
        print(
            "[ladon-matrix] ERROR: input rates must be greater than zero",
            file=sys.stderr,
        )
        return 2

    prepare_result_directories(orderers)
    combinations = [
        (orderer, input_rate, run_number)
        for orderer, input_rate in product(orderers, input_rates)
        for run_number in range(1, arguments.runs + 1)
    ]

    print("\nLadon CloudLab experiment matrix")
    print(f"  Input rates: {', '.join(map(str, input_rates))} req/s")
    print(f"  Orderers:    {', '.join(map(str, orderers))}")
    print(f"  Runs:        {arguments.runs} per combination")
    print(f"  Payload:     {PAYLOAD_SIZE} bytes")
    print(f"  Duration:    {DURATION} s")
    print(f"  Experiments: {len(combinations)}")
    for index, (orderer, input_rate, run_number) in enumerate(
        combinations,
        start=1,
    ):
        print(
            f"    [{index}] {orderer}, {input_rate} req/s, "
            f"run {run_number}/{arguments.runs}"
        )

    results: list[tuple[str, int, int, Path, Path]] = []
    try:
        for index, (orderer, input_rate, run_number) in enumerate(
            combinations,
            start=1,
        ):
            print(
                f"\n{'=' * 72}\n"
                f"Experiment {index}/{len(combinations)}: "
                f"{orderer}, {input_rate} req/s, "
                f"run {run_number}/{arguments.runs}\n"
                f"{'=' * 72}"
            )
            config = experiment_config(input_rate, orderer)
            result = runner.run_remote_config(
                config=config,
                controller_hostname=CONTROLLER_HOSTNAME,
                controller_private_hostname=CONTROLLER_PRIVATE_HOSTNAME,
                dry_run=arguments.dry_run,
            )
            if result is not None:
                result_file = write_result_file(
                    result,
                    config,
                    run_number=run_number,
                    total_runs=arguments.runs,
                )
                print(f"\nMatrix result file: {result_file}")
                results.append(
                    (orderer, input_rate, run_number, result, result_file)
                )
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"\n[ladon-matrix] ERROR: {error}", file=sys.stderr)
        return 1

    if arguments.dry_run:
        print("\nDry run completed; no experiments were started.")
    else:
        print("\nExperiment matrix completed:")
        for orderer, input_rate, run_number, raw_result, result_file in results:
            print(
                f"  {orderer}, {input_rate} req/s, run {run_number} "
                f"-> {result_file} "
                f"(raw: {raw_result})"
            )
        try:
            (
                csv_file,
                markdown_file,
                response_plot_file,
                scheduled_plot_file,
            ) = summarizer.generate_reports(
                result_root=RESULT_ROOT,
                minimum_input_rate=min(input_rates),
                maximum_input_rate=max(input_rates),
            )
            print("\nAggregate tables and plots:")
            print(f"  CSV:                {csv_file}")
            print(f"  Markdown:           {markdown_file}")
            print(f"  Response latency:   {response_plot_file}")
            print(f"  Scheduled latency:  {scheduled_plot_file}")
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            print(
                f"\n[ladon-matrix] WARNING: aggregate reports were not "
                f"generated: {error}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
