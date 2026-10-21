# Experiment: state-rebuild compaction for long sessions

Date: 2026-10-21. Tracking: [#5](https://github.com/plainIsaac/spaghetti-agent/issues/5).
Harness: [`benchmarks/long_session`](../../benchmarks/long_session/README.md).
Follows: [context economics](context-economics.md).

## Question

The context-economics experiment showed that pull-only activations lose to a
cached transcript: re-orienting on every fresh call costs far more reasoning
than the cache saves. The alternative tested here keeps the transcript as the
working context and uses runtime state only when the transcript must shrink:
at the context budget, or when resuming after a long pause. Does rebuilding
from state beat today's practice of asking the model to summarize?

**Pass criterion (set before running):** a rebuild arm scores at least as well
as summary compaction on late messages, at no higher total cost, with a smaller
resume penalty.

## Setup

- 150-message session with 139 graded messages. Facts, a read-only
  constraint, and two conventions are planted early; there are code edits,
  three long document reads, three cache-expiring pauses, and recall checks
  from #112 onward.
- Same model for every arm: `opencode-go/minimax-m3.1-flash-free` (free).
- Two context budgets: 24k tokens (2–3 compactions) and 12k tokens (5–9).
- One run per arm and budget.
- Cost is in units of one uncached input token at Anthropic-style prices
  (read 0.1×, write 1.25×, output 5×). It is priced from each call's exact
  cached prefix, with cold calls after each pause.

| Arm | On compaction or resume |
| --- | --- |
| summary | The model writes a summary of the conversation (current practice) |
| rebuild | The runtime rebuilds context from state with no model call: user messages, notes, changed files, REPL variables. `history.*` gives full turns and diffs |
| rebuild-notes | rebuild at low reasoning effort, told to save facts with `note()` |

## Results

| Budget | Arm | Score | Late (#100+) | Compactions | Cost | Resume cost | Wall time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 24k | summary | 132/139 | 44/50 | 2 | 894k | 71k | 25 min |
| 24k | **rebuild** | **138/139** | **49/50** | 3 | **743k** (−17%) | **28k** | 21 min |
| 24k | rebuild-notes | 134/139 | 48/50 | 3 | 376k (−58%) | 18k | 10 min |
| 12k | summary | 111/139 | 28/50 | 9 | 1,052k | 40k | 32 min |
| 12k | **rebuild** | **139/139** | **50/50** | 6 | **673k** (−36%) | **27k** | 23 min |
| 12k | rebuild-notes | 133/139 | 48/50 | 5 | 344k (−67%) | 23k | 11 min |

OpenAI-style prices give the same ordering: summary 1,452k, rebuild 904k, and
rebuild-notes 402k at 12k.

### Findings

1. **The pass criterion is met at both budgets.** Rebuild scored higher than
   summary, cost 17–36% less, and paid 33–61% less to resume after pauses.
2. **Summaries lose conventions first.** After its second compaction at 24k,
   the summary arm stopped reporting money in cents. At 12k it lost cents and
   the name format from about #81 onward. It then lost recall too: it claimed
   it had added no functions to `pricing.py`, reported the old Gold discount
   (12%, changed to 15% at #105), and misremembered the contract notice
   period. Each summary is a lossy copy of the previous one, so errors
   compound.
3. **Rebuild degrades less as compaction gets more frequent.** Going from 2–3
   to 6 compactions did not hurt it (138 → 139), while summary fell from 132 to
   111. User messages are kept verbatim, and file changes and notes are
   recorded mechanically rather than paraphrased.
4. **Summarizing costs money.** Each summary is model output (priced at 5×)
   and adds reasoning. The summary arm averaged 339–439 reasoning tokens per
   call against rebuild's 275–303. Rebuilding costs no model call at all.
5. **Low reasoning roughly halves the cost again but costs accuracy (option
   A).** rebuild-notes was the cheapest and fastest arm (~21 reasoning tokens
   per call), and it still beat summary on late recall. Its misses were
   careless: replying "I found the clause" without the answer, miscounting,
   and naming the wrong root-cause service. Notes alone do not replace
   reasoning. Low effort is a reasonable option for cheap turns, not the
   default.

## Verdict

**Option C with option B wins:** keep the cached transcript, and compact and
resume by rebuilding from durable runtime state instead of summarizing. On
this scenario it was both more accurate and cheaper than current practice. The
gap widened as compaction became more frequent, which is the forever-session
regime. This, not pull-only activations, is the architecture worth building
Spaghetti Agent around.

## Limits

- One model, one scenario, one run per cell. The direction is consistent
  across both budgets, but exact percentages will move.
- The rebuild keeps all user messages verbatim up to 16k characters. In a
  truly unbounded session that list also needs trimming, through search
  (`history.messages(query)`) or the agent's own notes. That case is
  untested.
- The summary prompt is a reasonable stand-in for current harnesses, not any
  particular product's prompt.
- Messages are flattened into one prompt for both arms, and caching is priced
  from exact shared prefixes rather than measured hits.
