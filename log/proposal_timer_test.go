package log

import (
	"sync"
	"testing"
	"time"
)

func TestProposalTimerMatchesLocalBatchExactlyOnce(t *testing.T) {
	var timer proposalTimer
	start := time.Now()
	timer.start(42, []byte("batch-a"), start)
	timer.start(42, []byte("batch-a"), start.Add(time.Millisecond))
	if _, ok := timer.finish(42, []byte("batch-b"), start.Add(2*time.Millisecond)); ok {
		t.Fatal("a different value at the same sequence number must not match")
	}
	if _, ok := timer.finish(41, []byte("batch-a"), start.Add(2*time.Millisecond)); ok {
		t.Fatal("a different sequence number must not match")
	}
	duration, ok := timer.finish(42, []byte("batch-a"), start.Add(4*time.Millisecond))
	if !ok || duration != 4*time.Millisecond {
		t.Fatalf("expected original proposal to local commit: %v, %v", duration, ok)
	}
	if _, ok := timer.finish(42, []byte("batch-a"), start.Add(5*time.Millisecond)); ok {
		t.Fatal("duplicate commit must not produce another sample")
	}
	if _, ok := timer.finish(43, []byte("remote-proposal"), start); ok {
		t.Fatal("follower commits must not produce proposer-local samples")
	}
}

func TestProposalTimerConcurrentCommitsOnlyCountOnce(t *testing.T) {
	var timer proposalTimer
	start := time.Now()
	timer.start(5, []byte("batch"), start)
	var group sync.WaitGroup
	results := make(chan bool, 20)
	for i := 0; i < 20; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			_, ok := timer.finish(5, []byte("batch"), start.Add(time.Second))
			results <- ok
		}()
	}
	group.Wait()
	close(results)
	matched := 0
	for ok := range results {
		if ok {
			matched++
		}
	}
	if matched != 1 {
		t.Fatalf("expected one sample, got %d", matched)
	}
}

func TestRecordProposalExcludesEmptyBatches(t *testing.T) {
	localProposals = proposalTimer{}
	RecordProposal(1, []byte("empty-batch"), 0)
	RecordProposal(2, nil, 10)
	if len(localProposals.starts) != 0 {
		t.Fatal("empty batches or missing digests must not start timing")
	}
	RecordProposal(3, []byte("nonempty"), 1)
	if _, ok := localProposals.finish(3, []byte("nonempty"), time.Now()); !ok {
		t.Fatal("nonempty local proposal should produce a duration")
	}
}
