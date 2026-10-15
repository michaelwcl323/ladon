"""Validate proposal durations through trace loading, SQL, summaries and plots."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import run_experiment as runner
import summarize_matrix_results as summary

REPO = Path(__file__).resolve().parents[1]


class ConsensusPipelineTests(unittest.TestCase):
    def analyze(self, root, durations):
        trace = root / "events.trc"
        events = [{"time": 1000000 + i, "message": "CONSENSUS_LATENCY",
                   "nodeId": 0, "sampledVal": i, "val0": value}
                  for i, value in enumerate(durations)]
        events += [{"time": 2000000, "message": "COMMIT", "nodeId": 0,
                    "sampledVal": 99, "val0": 0},
                   {"time": 3000000, "message": "REQ_FINISHED", "nodeId": 10,
                    "sampledVal": 1, "val0": 9000000}]
        trace.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        database = root / "events.sqlite"
        subprocess.run([sys.executable, str(REPO / "deployment/scripts/analyze/load-logs.py"),
                        str(database), str(trace)], check=True, capture_output=True)
        queries = (REPO / "deployment/queries/aggregates.sql").read_text()
        queries = queries[queries.index("-- Consensus latency:"):]
        sql = root / "consensus.sql"
        sql.write_text(queries)
        subprocess.run([sys.executable, str(REPO / "deployment/scripts/analyze/run-queries.py"),
                        str(database), str(sql), str(root)], check=True, capture_output=True)
        row = {path.stem: path.read_text().strip() for path in root.glob("*.val")}
        row.update({"exp": "0000", "orderer": "Pbft", "leader-policy": "Simple",
                    "peers": "10", "clients": "1", "throughput-raw": "39900",
                    "target-throughput": "40000", "duration-raw": "120",
                    "latency-avg-raw": "9000"})
        return row

    def test_trace_to_sql_to_summary_to_consensus_plot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = self.analyze(root, [10000, 20000, 30000])
            self.assertEqual(float(row["consensus-latency-avg-raw"]), 20)
            self.assertEqual(float(row["consensus-latency-95pctile-raw"]), 30)
            self.assertEqual(float(row["consensus-latency-stdev-raw"]), 10)
            self.assertEqual(int(row["consensus-samples-raw"]), 3)
            text = runner.format_result_summary_row(row, parameters=[("Input rate", "40000 req/s")])
            folder = root / f"{summary.PROTOCOL}-Pbft"
            folder.mkdir()
            path = folder / "new.txt"
            path.write_text(text)
            measured = summary.parse_result(path)
            self.assertEqual(measured.consensus_latency_ms, 20)
            self.assertEqual(measured.consensus_samples, 3)
            self.assertIn("Request latency (avg)", text)
            self.assertIn("9000.00 ms", text)
            outputs = summary.generate_reports(result_root=root,
                minimum_input_rate=40000, maximum_input_rate=40000)
            self.assertTrue(all(path.is_file() and path.stat().st_size for path in outputs))
            with outputs[0].open() as stream:
                plotted = next(csv.DictReader(stream))
            self.assertEqual(float(plotted["consensus_latency_ms"]), 20)

    def test_missing_consensus_samples_are_not_zero_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = self.analyze(root, [])
            text = runner.format_result_summary_row(row, parameters=[("Input rate", "40000 req/s")])
            path = root / "missing.txt"
            path.write_text(text)
            self.assertIn("Average consensus latency", text)
            self.assertIsNone(summary.parse_result(path))

    def test_legacy_request_latency_is_never_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.txt"
            path.write_text("| Average latency | 9999 ms |\n| Input rate | 40000 req/s |\n")
            self.assertIsNone(summary.parse_result(path))


if __name__ == "__main__":
    unittest.main()
