#!/usr/bin/env python3
"""Aggregate proposer-local consensus latency; never substitute request latency."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
import re
from statistics import fmean

PROTOCOL = "Ladon"
DEFAULT_RESULT_ROOT = Path(__file__).resolve().parent / "result"
DEFINITION = "propose -> proposer-local commit"
TABLE_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$")


@dataclass(frozen=True)
class Measurement:
    orderer: str
    input_rate: int
    actual_tps: float
    consensus_latency_ms: float
    consensus_samples: int


def parse_result(path: Path) -> Measurement | None:
    fields = {m[1].strip(): m[2].strip() for line in path.read_text().splitlines()
              if (m := TABLE_ROW.match(line))}
    if fields.get("Latency definition") != DEFINITION:
        return None
    latency = fields.get("Average consensus latency", "N/A")
    samples = fields.get("Consensus samples", "0")
    if latency == "N/A" or samples == "N/A" or float(samples.split()[0]) <= 0:
        return None
    def number(key: str) -> float:
        return float(fields[key].split()[0].replace(",", ""))
    return Measurement(
        fields.get("Orderer") or fields["Protocol"].split("/")[0].strip(),
        int(number("Input rate")), number("Actual throughput"),
        number("Average consensus latency"), int(number("Consensus samples")),
    )


def load_measurements(result_root: Path) -> list[Measurement]:
    measurements = [value for path in sorted(result_root.glob(f"{PROTOCOL}-*/*.txt"))
                    if (value := parse_result(path)) is not None]
    if not measurements:
        raise FileNotFoundError(
            f"No propose-to-commit measurements under {result_root}; rebuild peers and rerun experiments."
        )
    return measurements


def aggregate_measurements(measurements: list[Measurement], *,
                           minimum_input_rate: int, maximum_input_rate: int) -> list[dict]:
    groups = {}
    for value in measurements:
        if minimum_input_rate <= value.input_rate <= maximum_input_rate:
            groups.setdefault((value.orderer, value.input_rate), []).append(value)
    if not groups:
        raise ValueError("No consensus measurements in the requested input-rate range")
    return [{"orderer": orderer, "input_rate_tx_s": rate, "runs": len(values),
             "actual_tps": fmean(v.actual_tps for v in values),
             "consensus_latency_ms": fmean(v.consensus_latency_ms for v in values),
             "consensus_samples": sum(v.consensus_samples for v in values)}
            for (orderer, rate), values in sorted(groups.items())]


def generate_reports(*, result_root: Path = DEFAULT_RESULT_ROOT,
                     minimum_input_rate: int = 20000,
                     maximum_input_rate: int = 80000) -> tuple[Path, Path, Path]:
    if minimum_input_rate > maximum_input_rate:
        raise ValueError("minimum input rate cannot exceed maximum input rate")
    rows = aggregate_measurements(load_measurements(result_root),
                                  minimum_input_rate=minimum_input_rate,
                                  maximum_input_rate=maximum_input_rate)
    suffix = f"{minimum_input_rate}-{maximum_input_rate}"
    csv_path = result_root / f"consensus-aggregate-{suffix}.csv"
    md_path = result_root / f"consensus-aggregate-{suffix}.md"
    png_path = result_root / f"tps-consensus-latency-{suffix}.png"
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = [f"# {PROTOCOL} consensus latency", "", DEFINITION, "",
             "One nonempty committed batch is one sample; run means are averaged equally.", "",
             "| Orderer | Input rate | Runs | Actual TPS | Consensus latency (ms) | Samples |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['orderer']} | {row['input_rate_tx_s']} | {row['runs']} | "
                     f"{row['actual_tps']:.2f} | {row['consensus_latency_ms']:.3f} | "
                     f"{row['consensus_samples']} |")
    md_path.write_text("\n".join(lines) + "\n")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5))
    for orderer in sorted({r["orderer"] for r in rows}):
        values = [r for r in rows if r["orderer"] == orderer]
        ax.plot([r["actual_tps"] for r in values], [r["consensus_latency_ms"] for r in values],
                marker="o", label=f"{PROTOCOL}-{orderer}")
    ax.set(xlabel="Achieved throughput (tx/s)", ylabel="Mean propose-to-local-commit latency (ms)",
           title=f"{PROTOCOL}: propose → commit", ylim=(0, None))
    ax.grid(alpha=.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(png_path, dpi=200)
    plt.close(fig)
    return csv_path, md_path, png_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, default=DEFAULT_RESULT_ROOT)
    parser.add_argument("--min-input-rate", type=int, default=20000)
    parser.add_argument("--max-input-rate", type=int, default=80000)
    args = parser.parse_args()
    for path in generate_reports(result_root=args.result_root,
                                 minimum_input_rate=args.min_input_rate,
                                 maximum_input_rate=args.max_input_rate):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
