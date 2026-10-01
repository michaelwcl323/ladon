#!/usr/bin/env python3
"""Aggregate Ladon matrix results and create a throughput-latency plot."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import os
from pathlib import Path
import re
from statistics import fmean
import sys
import tempfile


DEFAULT_RESULT_ROOT = Path(__file__).resolve().parent / "result"
DEFAULT_RAW_RESULT_ROOT = (
    Path(__file__).resolve().parents[1] / "deployment/deployment-data"
)
TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$")


@dataclass(frozen=True)
class Measurement:
    orderer: str
    input_rate: int
    actual_tps: float
    average_latency_ms: float
    schedule_lateness_ms: float


@dataclass(frozen=True)
class Aggregate:
    orderer: str
    input_rate: int
    actual_tps_values: tuple[float, ...]
    latency_values_ms: tuple[float, ...]
    schedule_lateness_values_ms: tuple[float, ...]

    @property
    def runs(self) -> int:
        return len(self.actual_tps_values)

    @property
    def average_actual_tps(self) -> float:
        return fmean(self.actual_tps_values)

    @property
    def average_latency_ms(self) -> float:
        return fmean(self.latency_values_ms)

    @property
    def average_schedule_lateness_ms(self) -> float:
        return fmean(self.schedule_lateness_values_ms)

    @property
    def schedule_to_finish_values_ms(self) -> tuple[float, ...]:
        return tuple(
            latency + lateness
            for latency, lateness in zip(
                self.latency_values_ms,
                self.schedule_lateness_values_ms,
            )
        )

    @property
    def average_schedule_to_finish_ms(self) -> float:
        return fmean(self.schedule_to_finish_values_ms)


def parse_number(value: str, field: str, path: Path) -> float:
    try:
        return float(value.split()[0].replace(",", ""))
    except (IndexError, ValueError) as error:
        raise ValueError(f"Invalid {field} in {path}: {value!r}") from error


def measurement_key(
    orderer: str,
    input_rate: int,
    actual_tps: float,
    average_latency_ms: float,
) -> tuple[str, int, float, float]:
    return (
        orderer,
        input_rate,
        round(actual_tps, 2),
        round(average_latency_ms, 2),
    )


def load_schedule_lateness(
    raw_result_root: Path,
) -> dict[tuple[str, int, float, float], float]:
    """Index mean client schedule lateness using raw result-summary files."""

    index = {}
    for path in sorted(raw_result_root.glob("remote-*/result-summary.csv")):
        with path.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        if not rows:
            continue
        row = rows[0]
        try:
            orderer = row["orderer"]
            input_rate = int(row["target-throughput"])
            actual_tps = float(row["throughput-raw"])
            average_latency_ms = float(row["latency-avg-raw"])
            client_slack_us = float(row["client-slack-avg-raw"])
        except (KeyError, TypeError, ValueError):
            continue
        key = measurement_key(
            orderer,
            input_rate,
            actual_tps,
            average_latency_ms,
        )
        if key in index:
            raise ValueError(
                "Ambiguous raw result match for "
                f"{orderer}, input rate {input_rate}, TPS {actual_tps:.2f}"
            )
        index[key] = max(0.0, -client_slack_us / 1000.0)
    return index


def parse_result(
    path: Path,
    schedule_lateness: dict[tuple[str, int, float, float], float],
) -> Measurement:
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = TABLE_ROW.match(line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()

    required = ("Input rate", "Actual throughput", "Average latency")
    missing = [field for field in required if field not in fields]
    if missing:
        raise ValueError(f"{path} is missing: {', '.join(missing)}")

    orderer = fields.get("Orderer")
    if not orderer:
        protocol = fields.get("Protocol", "")
        orderer = protocol.split("/", 1)[0].strip()
    if not orderer:
        orderer = path.parent.name.removeprefix("Ladon-")

    input_rate = int(parse_number(fields["Input rate"], "Input rate", path))
    actual_tps = parse_number(
        fields["Actual throughput"],
        "Actual throughput",
        path,
    )
    average_latency_ms = parse_number(
        fields["Average latency"],
        "Average latency",
        path,
    )
    key = measurement_key(orderer, input_rate, actual_tps, average_latency_ms)
    if key not in schedule_lateness:
        raise ValueError(
            f"Could not match {path} to a raw result-summary.csv row"
        )
    return Measurement(
        orderer=orderer,
        input_rate=input_rate,
        actual_tps=actual_tps,
        average_latency_ms=average_latency_ms,
        schedule_lateness_ms=schedule_lateness[key],
    )


def load_measurements(
    result_root: Path,
    raw_result_root: Path,
) -> list[Measurement]:
    paths = sorted(result_root.glob("Ladon-*/*.txt"))
    if not paths:
        raise FileNotFoundError(f"No matrix result files found under {result_root}")
    schedule_lateness = load_schedule_lateness(raw_result_root)
    if not schedule_lateness:
        raise FileNotFoundError(
            f"No usable raw result summaries found under {raw_result_root}"
        )
    return [parse_result(path, schedule_lateness) for path in paths]


def aggregate_measurements(
    measurements: list[Measurement],
    *,
    minimum_input_rate: int,
    maximum_input_rate: int,
) -> list[Aggregate]:
    grouped: dict[tuple[str, int], list[Measurement]] = {}
    for measurement in measurements:
        if minimum_input_rate <= measurement.input_rate <= maximum_input_rate:
            key = (measurement.orderer, measurement.input_rate)
            grouped.setdefault(key, []).append(measurement)

    if not grouped:
        raise ValueError(
            "No results fall within input-rate range "
            f"{minimum_input_rate}–{maximum_input_rate}"
        )

    aggregates = []
    for (orderer, input_rate), values in sorted(grouped.items()):
        aggregates.append(
            Aggregate(
                orderer=orderer,
                input_rate=input_rate,
                actual_tps_values=tuple(value.actual_tps for value in values),
                latency_values_ms=tuple(
                    value.average_latency_ms for value in values
                ),
                schedule_lateness_values_ms=tuple(
                    value.schedule_lateness_ms for value in values
                ),
            )
        )
    return aggregates


def values_text(values: tuple[float, ...]) -> str:
    return " / ".join(f"{value:.2f}" for value in values)


def write_csv(aggregates: list[Aggregate], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(
            [
                "orderer",
                "input_rate_req_s",
                "runs",
                "actual_tps_values",
                "average_actual_tps",
                "average_latency_values_ms",
                "average_latency_ms",
                "schedule_lateness_values_ms",
                "average_schedule_lateness_ms",
                "schedule_to_finish_values_ms",
                "average_schedule_to_finish_ms",
            ]
        )
        for aggregate in aggregates:
            writer.writerow(
                [
                    aggregate.orderer,
                    aggregate.input_rate,
                    aggregate.runs,
                    values_text(aggregate.actual_tps_values),
                    f"{aggregate.average_actual_tps:.3f}",
                    values_text(aggregate.latency_values_ms),
                    f"{aggregate.average_latency_ms:.3f}",
                    values_text(aggregate.schedule_lateness_values_ms),
                    f"{aggregate.average_schedule_lateness_ms:.3f}",
                    values_text(aggregate.schedule_to_finish_values_ms),
                    f"{aggregate.average_schedule_to_finish_ms:.3f}",
                ]
            )


def write_markdown(aggregates: list[Aggregate], output: Path) -> None:
    lines = [
        "# Ladon throughput-latency summary",
        "",
        "| Orderer | Input rate (req/s) | Runs | Actual TPS values | "
        "Average actual TPS | Average latency values (ms) | "
        "Average latency (ms) | Average schedule lateness (ms) | "
        "Average schedule-to-finish latency (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for aggregate in aggregates:
        lines.append(
            f"| {aggregate.orderer} "
            f"| {aggregate.input_rate} "
            f"| {aggregate.runs} "
            f"| {values_text(aggregate.actual_tps_values)} "
            f"| {aggregate.average_actual_tps:.3f} "
            f"| {values_text(aggregate.latency_values_ms)} "
            f"| {aggregate.average_latency_ms:.3f} "
            f"| {aggregate.average_schedule_lateness_ms:.3f} "
            f"| {aggregate.average_schedule_to_finish_ms:.3f} |"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_plot(
    aggregates: list[Aggregate],
    output: Path,
    *,
    minimum_input_rate: int,
    maximum_input_rate: int,
    schedule_to_finish: bool = False,
) -> None:
    cache = Path(tempfile.gettempdir()) / f"ladon-matplotlib-{os.getuid()}"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    grouped: dict[str, list[Aggregate]] = {}
    for aggregate in aggregates:
        grouped.setdefault(aggregate.orderer, []).append(aggregate)

    styles = [
        ("#2563eb", "o"),
        ("#dc2626", "s"),
        ("#059669", "^"),
        ("#7c3aed", "D"),
    ]
    figure, axis = plt.subplots(figsize=(9, 6), dpi=160)
    for index, (orderer, values) in enumerate(sorted(grouped.items())):
        values.sort(key=lambda item: item.input_rate)
        color, marker = styles[index % len(styles)]
        x_values = [value.average_actual_tps for value in values]
        y_values = [
            (
                value.average_schedule_to_finish_ms
                if schedule_to_finish
                else value.average_latency_ms
            )
            for value in values
        ]
        axis.plot(
            x_values,
            y_values,
            color=color,
            marker=marker,
            linewidth=2,
            markersize=7,
            label=f"Ladon-{orderer}",
        )
        for value_index, value in enumerate(values):
            if value_index == len(values) - 2:
                offset = (-8, 9)
                horizontal_alignment = "right"
                vertical_alignment = "bottom"
            elif value_index == len(values) - 1:
                offset = (8, -9)
                horizontal_alignment = "left"
                vertical_alignment = "top"
            else:
                offset = (5, 6)
                horizontal_alignment = "left"
                vertical_alignment = "bottom"
            axis.annotate(
                f"{value.input_rate // 1000}k",
                (
                    value.average_actual_tps,
                    (
                        value.average_schedule_to_finish_ms
                        if schedule_to_finish
                        else value.average_latency_ms
                    ),
                ),
                xytext=offset,
                textcoords="offset points",
                fontsize=8,
                color=color,
                ha=horizontal_alignment,
                va=vertical_alignment,
            )

    axis.set_xlabel("Average actual TPS (req/s)")
    if schedule_to_finish:
        axis.set_ylabel("Average schedule-to-finish latency (ms)")
        title_metric = "Throughput–Schedule-to-Finish Latency"
    else:
        axis.set_ylabel("Average response latency (ms)")
        title_metric = "Throughput–Response Latency"
    axis.set_title(
        f"Ladon {title_metric} "
        f"(input rate {minimum_input_rate:,}–{maximum_input_rate:,} req/s)"
    )
    axis.grid(True, linestyle="--", alpha=0.35)
    axis.legend()
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, bbox_inches="tight")
    plt.close(figure)


def generate_reports(
    *,
    result_root: Path = DEFAULT_RESULT_ROOT,
    raw_result_root: Path = DEFAULT_RAW_RESULT_ROOT,
    minimum_input_rate: int = 20000,
    maximum_input_rate: int = 80000,
) -> tuple[Path, Path, Path, Path]:
    if minimum_input_rate > maximum_input_rate:
        raise ValueError("minimum input rate cannot exceed maximum input rate")

    aggregates = aggregate_measurements(
        load_measurements(result_root, raw_result_root),
        minimum_input_rate=minimum_input_rate,
        maximum_input_rate=maximum_input_rate,
    )
    suffix = f"{minimum_input_rate}-{maximum_input_rate}"
    csv_output = result_root / f"aggregate-{suffix}.csv"
    markdown_output = result_root / f"aggregate-{suffix}.md"
    plot_output = result_root / f"tps-latency-{suffix}.png"
    adjusted_plot_output = (
        result_root / f"tps-schedule-to-finish-{suffix}.png"
    )

    write_csv(aggregates, csv_output)
    write_markdown(aggregates, markdown_output)
    write_plot(
        aggregates,
        plot_output,
        minimum_input_rate=minimum_input_rate,
        maximum_input_rate=maximum_input_rate,
    )
    write_plot(
        aggregates,
        adjusted_plot_output,
        minimum_input_rate=minimum_input_rate,
        maximum_input_rate=maximum_input_rate,
        schedule_to_finish=True,
    )
    return csv_output, markdown_output, plot_output, adjusted_plot_output


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result-root",
        type=Path,
        default=DEFAULT_RESULT_ROOT,
        help="matrix result root",
    )
    parser.add_argument(
        "--raw-result-root",
        type=Path,
        default=DEFAULT_RAW_RESULT_ROOT,
        help="deployment-data root containing raw result summaries",
    )
    parser.add_argument("--min-input-rate", type=int, default=20000)
    parser.add_argument("--max-input-rate", type=int, default=80000)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        outputs = generate_reports(
            result_root=arguments.result_root.resolve(),
            raw_result_root=arguments.raw_result_root.resolve(),
            minimum_input_rate=arguments.min_input_rate,
            maximum_input_rate=arguments.max_input_rate,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print(f"[ladon-summary] ERROR: {error}", file=sys.stderr)
        return 1

    print("Generated matrix reports:")
    for output in outputs:
        print(f"  {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
