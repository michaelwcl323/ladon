# Consensus latency

The primary latency for PBFT and HotStuff is now proposal broadcast → local
final commit **at the proposer**, measured with Go's monotonic clock. A sample
must match both the sequence number and batch digest. Empty batches, aborted
values, follower commits and duplicates are excluded. A retransmission retains
the first proposal time for that value. Consensus messages, quorum rules and
commit decisions are unchanged.

The peer emits `CONSENSUS_LATENCY` with duration in microseconds in `val0`.
The trace loader imports this event into the protocol table. SQL exports
`consensus-latency-avg-raw`, `consensus-latency-95pctile-raw`,
`consensus-latency-stdev-raw` (all milliseconds), and `consensus-samples-raw`.
Each nonempty committed batch is one sample; `TraceSampling` still applies
by sequence number. The existing request latency is preserved separately.

Experiment summaries label the primary value `Average consensus latency` and
include `Latency definition: propose -> proposer-local commit`. The matrix
report uses only these marked consensus results. It does not add client
schedule lateness, and it rejects legacy request-only results. If there are
no consensus samples, the displayed latency is N/A rather than zero.

Rebuild peers on the experiment machines and rerun to collect this event.
Existing summary text files cannot supply the new metric. Reanalysis of an
existing trace database requires rebuilding the database and queries (`-f all`),
and only works if the original trace already contains `CONSENSUS_LATENCY`.

`ProposeTs` and `CommitTs` used by PBFT leader control retain their existing
meaning. The incorrect constant-zero `Committed entry.latency` field has been
removed; valid proposer samples are logged as `consensus_latency_ms` instead.
