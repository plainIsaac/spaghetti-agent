# Experiment: does pull-based context pay off once caching is priced in?

Date: 2026-10-21. Harness: [`benchmarks/context_economics`](../../benchmarks/context_economics/README.md).

## Question

Spaghetti Agent replaces the replayed transcript with small activations and
state the agent pulls from the runtime. Prompt caching makes replaying a stable
prefix cheap (reads ≈ 0.1× input) while writing new prefix costs ≥ 1×. So the
runtime should only win where the cache is absent or keeps expiring: very long
("forever") sessions, separately managed chunks, or resuming after a long gap.
Does it, and at what point?

## Setup

- One scripted 12-message session with 10 automatically graded requests (CSV
  questions, reading a long report, two code fixes checked by pytest, a fact to
  remember, a convention to keep, and a cache-expiring pause before message 9).
- Same model for every arm: `opencode-go/minimax-m3.1-flash-free` via the new
  `--opencode` provider (free tier; the ChatGPT subscription window was
  exhausted during setup, and Claude has no usage-free path).
- Arms: `transcript` (conventional code-act loop), `runtime` (Spaghetti as
  shipped), `runtime-continue` (re-triggered while unanswered), and
  `runtime-observe` (re-triggered and shown the previous program's output, the
  new opt-in `observe_program_output`).
- Costs are in units of one uncached input token, with the same idealized
  prefix cache for every arm (see `report.py`). Anthropic-style prices: write
  1.25×, read 0.1×, output 5×. OpenAI-style: write 1×, read 0.1×, output 8×.

## Results

| Arm | Runs | Score (of 10) | Calls | Prompt tok | Output tok (reasoning) | Cost, warm cache | Cost, cold resume | Cost, no cache | Wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| transcript | 2 | **10.0** | 31 | 233k | 4.6k (1.7k) | **57k** | **69k** | 256k | 2 min |
| runtime (as shipped) | 1 | 1.0 | 12 | 48k | 7.1k (6.1k) | 49k | 53k | 83k | 2 min |
| runtime-continue | 1 | 0.0 | 50 | 211k | 41k (34k) | 261k | 266k | 418k | 7 min |
| runtime-observe | 3 | 8.3 | 43 | 213k | 54k (44k) | 348k | 352k | 482k | 12 min |

Costs use Anthropic-style prices; OpenAI-style prices give the same ordering
(transcript 68k warm vs runtime-observe 508k).

### Findings

1. **As shipped, the runtime answers almost nothing (1/10).** Models inspect
   before acting (`print(inbox.pending(), workspace.list())`). The kernel
   discarded that output and no further turn was scheduled, so the user got
   silence. Re-triggering without the output (`runtime-continue`) only loops:
   0/10 at 4× the calls.
2. **With one observation fed back, accuracy recovers to 8.3/10** but still
   trails the transcript. Its consistent miss is self-knowledge: asked "which
   files have you changed?", it re-explores the workspace for six turns and
   never answers. The transcript simply remembers.
3. **The cost driver is reasoning, not input.** A fresh activation makes the
   model re-orient every call: ~1,030 reasoning tokens per call vs ~56 in the
   transcript arm. Output makes up 77% of the runtime's cost but 40% of the
   transcript's.
4. **Caching neutralizes the input saving.** The runtime sends ~9% fewer prompt
   tokens, but the transcript's prompt is ~95% cache reads, so its input costs
   about a tenth of nominal. Even with no cache at all, the transcript (256k)
   is cheaper than runtime-observe (348k).
5. **The long-pause case is real but small.** Expiring the cache before
   message 9 costs the transcript +12.5k units and the runtime +4.2k. The
   runtime saves ~8k per long pause but spends ~290k more over the session.

### Projection to long sessions

Using the measured shape: transcript prompts grow ~770 tokens per message, and
runtime-observe costs ~28.6k units per message regardless of length.
Compaction is not modelled.

| Message # | Transcript, warm cache | Transcript, cold every message | Runtime-observe |
| ---: | ---: | ---: | ---: |
| 12 | 5.6k | 17.8k | 28.6k |
| 50 | 13.2k | 59.2k | 28.6k |
| 100 | 23.2k | 113.6k | 28.6k |
| 200 | 43.2k | 222.5k | 28.6k |

Break-even per message is around message 125 with a warm cache. If *every*
message arrives after the cache has expired, it is around message 25. Real
harnesses compact long before 125 messages, which lowers the transcript line
further.

## Verdict

The hypothesis holds in direction but fails in magnitude. Pull-based context
only wins on cost where caching cannot help: sessions running for hundreds of
messages, or traffic where the cache is cold on nearly every message. Even
there it gives up accuracy, because a stateless agent forgets its own recent
actions. At realistic session lengths it is about 6× more expensive and
slower. The dominant cost is the model re-deriving its situation from a cold,
instruction-heavy prompt, which input-token accounting does not capture.

What still looks worth testing is narrower: keep a cached transcript as the
working context, and use the durable runtime state as the **resume and
compaction mechanism**. That means rebuilding a small context from state after
a long gap or at the context limit, instead of summarizing the transcript.
That targets the only regime where the runtime won, without paying the
re-orientation cost on every turn.

## Limits

- One free model, one scenario, 12 messages, 1–3 runs per arm. The
  qualitative gaps are large; the exact ratios are not.
- The runtime's 3.5k-token instruction prompt was written for other models.
  A tighter prompt could reduce re-orientation reasoning, but the transcript
  arm still wins at this length even with zero reasoning overhead (~140k vs
  57k), because the runtime makes more calls with uncached tails.
- opencode keys provider caches per session, so measured cache hits are
  reported but not priced. The idealized model gives the runtime a cached
  system prompt it did not always get.
- Compaction is not modelled; it would favour the transcript.
