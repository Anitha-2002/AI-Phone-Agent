# Acceptance targets (starting points: tune after you have measurements)

| Area | Target | How measured | Measured |
| --- | --- | --- | --- |
| Latency | p95 end of caller speech to first agent audio < 1.5 s | Per-turn log (Phase 3) | |
| Interruption | Agent audio stops within 300-500 ms of a real interruption | Manual + test set | |
| Interruption | Coughs and "uh-huh" do not cut the agent off | Test set | |
| History | After an interruption, the model's history contains only what was played | Inspect context | |
| Transcription | WER target: ____ % on the 8 kHz telephony test set | Compare transcripts to references | |
| Failure | Caller never left in silence > ____ s when a dependency fails | Fault injection (Phase 3) | |
| Concurrency | p95 latency holds at ____ simultaneous calls | Load test (Phase 6) | |

Fill the blanks from your real call volume and business needs. Measure on hosted services first; numbers will change when you move to local models.
