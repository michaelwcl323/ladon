package log

import (
	"sync"
	"time"
)

type proposalKey struct {
	sn     int32
	digest string
}

type proposalTimer struct {
	sync.Mutex
	starts map[proposalKey]time.Time
}

var localProposals proposalTimer

// RecordProposal starts timing on this proposer immediately before broadcast.
// Receiving another node's proposal must never call this function.
func RecordProposal(sn int32, digest []byte, requests int) {
	if requests > 0 && len(digest) > 0 {
		localProposals.start(sn, digest, time.Now())
	}
}

func (p *proposalTimer) start(sn int32, digest []byte, now time.Time) {
	p.Lock()
	defer p.Unlock()
	if p.starts == nil {
		p.starts = make(map[proposalKey]time.Time)
	}
	key := proposalKey{sn, string(digest)}
	// A retransmission does not restart the original proposal's clock.
	if _, exists := p.starts[key]; !exists {
		p.starts[key] = now
	}
}

func (p *proposalTimer) finish(sn int32, digest []byte, now time.Time) (time.Duration, bool) {
	p.Lock()
	defer p.Unlock()
	key := proposalKey{sn, string(digest)}
	start, exists := p.starts[key]
	if !exists {
		return 0, false
	}
	delete(p.starts, key)
	// Local time.Time subtraction uses Go's monotonic clock.
	elapsed := now.Sub(start)
	return elapsed, elapsed >= 0
}
