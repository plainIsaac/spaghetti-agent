# Experiment: prompt quality, model strength, and where reasoning happens

Date: 2026-10-22. Follows [context economics](context-economics.md) and
[long-session compaction](long-session-compaction.md). Tracking: #5.

## Questions

Three objections to the context-economics verdict (pull-only activations cost
~6× a cached transcript):

1. The runtime's 3.5k-token instruction prompt is bad and drags performance down.
2. Stronger models are better equipped for a pull environment.
3. Reasoning models think differently when every call starts fresh: provider
   reasoning does not carry over, so moving the context may move the thinking.

## Setup

- Same 12-message, 10-graded scenario as context economics.
- New arms:
  - `runtime-observe-minimal`: the runtime with a ~400-token prompt covering
    only the APIs the task needs.
  - `transcript-flat`: the same conversation as `transcript`, but sent whole to
    a fresh session each call. Nothing the provider keeps between turns
    (such as reasoning) carries over.
- Models:
  - `opencode-go/minimax-m3.1-flash-free` (free, weak).
  - `openai/gpt-6.1-sol` at low, medium and high reasoning effort, through a
    ChatGPT subscription. The queue runner (`benchmarks/matrix.py`) paused at
    the usage limit and was resumed after the reset by a scheduled task.
- Every arm gets the same budget of 8 programs per message.
  - The first high-effort runtime run used 5 and scored 7/10. It ran out of
    turns because high effort takes smaller steps. Its rerun at 8 is reported
    below.
- Cost is in units of one uncached input token at Anthropic-style prices
  (read 0.1×, write 1.25×, output 5×) with a warm cache.

## Results

| Model / effort | Arm | Score | Calls | Reasoning per call | Cost (warm) |
| --- | --- | ---: | ---: | ---: | ---: |
| free | transcript | 10.0 (n=2) | 31 | 56 | 57k |
| free | transcript-flat | 10.0 (n=2) | 28 | 194 | 94k |
| free | runtime-observe, original prompt | 8.3 (n=3) | 43 | 1,029 | 348k |
| free | runtime-observe, minimal prompt | 8.5 (n=2: 10, 7) | 54 | 304 | 167k |
| sol medium | transcript | 9 | 20 | 7 | 30k |
| sol medium | transcript-flat | 10 | 20 | 29 | 34k |
| sol medium | runtime-observe, original prompt | 8 | 37 | 1 | 57k |
| sol medium | runtime-observe, minimal prompt | 9.5 (n=2: 10, 9) | 38 | 95 | 64k |
| sol high | transcript | 10 | 17 | 89 | 35k |
| sol high | transcript-flat | 9 | 14 | 83 | 30k |
| sol high | runtime-observe, minimal prompt | 9 | 48 | 118 | 95k |
| sol low | transcript | 9 | 19 | 2 | 20k |
| sol low | transcript-flat | 9 | 19 | 11 | 29k |
| sol low | runtime-observe, minimal prompt | 9 | 44 | 203 | 96k |

## Verdict

1. **The prompt was a real drag (confirmed).** The minimal prompt halved the
   runtime's cost on the free model (348k → 167k) and cut reasoning per call
   from ~1,030 to ~300 tokens. On the strong model it raised the score from 8
   to 9.5 and halved prompt tokens (157k → 75k). The minimal prompt should
   replace the current one for single-agent work.
2. **Stronger models close the accuracy gap and most of the cost gap
   (mostly confirmed).** With a good model and the minimal prompt, the runtime
   matches the transcript on accuracy: 9–10 against 9–10. Its cost overhead
   falls from ~6× (free model, original prompt) to ~2× at medium effort. It
   does not disappear: the runtime makes about twice as many calls (inspect,
   then answer), and every call carries an uncached activation tail. On short
   sessions that, not reasoning, is now the main cost.
3. **Losing reasoning carry-over costs tokens, not accuracy (partly
   confirmed).** Stateless calls reasoned 3–4× more per call than native
   sessions with the free model and at medium effort. At high effort the
   difference vanished (83 vs 89). Accuracy was unaffected in every pair.
   - Effect on the long-session experiment: both of its arms ran stateless, so
     the absolute costs there are somewhat inflated for both. The comparison
     between them still holds.
   - The earlier high-effort runtime drop was the turn budget, not thinking
     location (7 → 9 with the same budget).

**Overall:** the objections were right that the first experiment was unfair to
the runtime: a bad prompt and a weak model. Fixing both makes pull-only
activations accurate but still about 2× the cost of a cached transcript in
short sessions. The recommendation stands, sharpened:

- Use the cached transcript as the working context.
- Rebuild from runtime state at compaction and on resume, which won on both
  accuracy and cost in the long-session experiment.
- Adopt the minimal prompt.
- Prefer strong models in pull mode.

## Limits

- One or two runs per cell, one scenario, 12 messages, and one strong model.
  Differences of one point are within noise.
- Wall-clock times are omitted: ChatGPT subscription latency varied from ~4 s
  to ~150 s per call during the runs.
- Reasoning token counts are as reported through opencode. The anomalously
  high runtime reasoning at low effort (203 per call) is unexplained.
