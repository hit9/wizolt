# Context

Long tasks build up conversation, code and tool output. wizolt makes room automatically so you
can keep working. **`/status`** shows how much context is in use and how many tokens you have spent.

## What the model receives

Each request includes your instructions, the conversation so far and the tools currently
available. Disconnected MCP servers add nothing. Large tool results are shortened; the agent
can read the full output when it needs it.

Provider-side search is billed separately by most providers, and its pages are not included
in the context meter.

## Keeping context manageable

### Compaction

As context fills up, wizolt summarizes older conversation and keeps **about eight recent
messages**. The task continues. Summaries can lose detail, so restate an important constraint
if the agent seems to have forgotten it.

```{figure} _static/context-compaction.svg
:class: concept-diagram
:alt: Concept diagram showing older conversation replaced by a summary while about eight recent messages remain unchanged.

Keep recent details; summarize older conversation.
```

Your messages and the agent's replies are also saved in history files for the agent to look up.
The newest **50** compacted spans keep these files.

Run **`/compact`** to summarize now, such as before starting a large refactor.
Use **`/compact log`** to review past summaries, or `/compact log seg.N` to print one.

### Starting a new window

Run **`/context reset`** for a fresh model window. wizolt keeps the working notes and recent
activity, your visible transcript, background jobs and workspace.
The agent can also schedule a reset after its current turn; the divider shows `reset pending`
until it happens.

### When a summary does not arrive

If summarizing fails, wizolt still makes room and saves the earlier conversation, but carries
less detail forward. It reports the reason; remind the agent what matters before continuing.
Check [Compaction model](configuration.md#compaction-model) if this happens often.

A [PreCompact hook](hooks.md#events) can refuse compaction. An automatic refusal stops the
turn so you can resolve it before continuing.

## Spending less

- Keep the same model and connected tools during a task to improve cache reuse.
- Give summaries a cheaper model with [compaction settings](configuration.md#compaction-model).
  Check `/status` to compare its usage and cache hit rate.
- Delegate a bounded task to a cheaper [worker](worker.md).
- Lower `runtime.max_context_tokens` if you prefer smaller requests and more frequent summaries.

## Prompt caching

A provider can reuse the unchanged beginning of a request. A higher cache hit rate usually
means less input cost.

```{figure} _static/context-cache.svg
:class: concept-diagram
:alt: Concept diagram comparing a previous request with the next request, which shares its beginning and adds new input.

An unchanged beginning gives the provider more to reuse.
```

Switching models, connecting tools or compacting context can lower reuse. Cache support and
pricing depend on your provider.

### Checking the hit rate

**`/status`** shows the latest request and whole-session hit rates. The latest rate also appears
in the statusbar.

<div class="term-shot" role="img" aria-label="Two rows of /status: a cache row with a fill meter, the latest request's hit ratio and the session's, then a usage row with the call count and total tokens."><span><span class="fs-i fs-dim">cache</span>  <span class="fs-i fs-add">[████████████▊░]</span> last <span class="fs-i fs-sel">95.9%</span>; session <span class="fs-i fs-add">91.5%</span></span><span><span class="fs-i fs-dim">usage</span>  calls 14; total 182.3K</span></div>
