# Main and subagent state ownership

This is a maintenance contract, not a user guide. Read it before moving mutable state between
Session, engine, frontend or shared capability services. The defining distinction is **independent
conversations in one shared filesystem**. See [Parallel agents](DESIGN.md#parallel-agents).

## Invariants

1. One engine writes one Session on its owning event loop. Accepted input enters that Session's
   inbox; a UI switch never changes its destination. Workers return receipts and immutable source
   drafts; the loop publishes them in tool-call order.
2. Spawn copies configuration values and starts with only the assigned task. It copies no parent
   messages, notes, plan, summary, active skills, usage, tool records or pending inputs. Nested
   agents obey the same rule and join the same group-wide admission limit.
3. Share workspace discovery and external capabilities, not what a conversation knows or how its
   frontend responds. A callback capturing an agent/frontend makes its containing object local.
4. Keys such as `tr.1`, `view.1`, `seg.1` and `job.1` are local. Resolve them with their owning
   Session, never the selected agent or a process-wide registry. Filesystem artifacts are UID-scoped.
5. The root owns the family lease and shared service shutdown. Stop admission and join engines
   before closing clients/jobs, then close shared MCP and release the lease. Closing a child must
   not close the root's capabilities. Jobs survive turns, not application-session shutdown.

## Session field inventory

Every instance field is classified below. Class constants contain policy, not agent state.
“Durable” means selected by the codec/store; runtime handles are rebuilt rather than serialized.

| Fields | Owner and lifetime |
| --- | --- |
| `uid`, `created_at`, `cwd`, `agent_name`, `agent_parent`, `context_layout_version` | Local durable identity. Children use a family-prefixed UID and the same cwd. Creation retains the family's timestamp. |
| `config`, `settings`, `provider_overrides` | Local detached values. Only provider/model/effort/API overrides are durable; a child freezes inherited choices too. Credentials and ordinary `/set` runtime values come from current startup configuration. Removed provider entries fall back to current configuration; no credentials enter snapshots. |
| `system_info` | Shared, read-only-in-use startup environment/instruction snapshot. No conversation knowledge or callbacks. Live instruction references use the local resolver. |
| `system_prompt`, `tool_names`, `listed`, `resumed` | Local assembly metadata. Child workspace instructions are added on attachment, not persisted repeatedly. Children stay out of standalone listings/latest pointers and resume through the root. |
| `messages`, `state`, `active_skills`, `context_reset_requested` | Local durable conversation/working memory. Reset and compaction affect this agent only. Skill activation controls this agent's tool permissions and hooks. |
| `pending_user_inputs` | Local durable inbox, including held next-turn inputs, images and frontend command provenance. Model-authored tasks never execute CLI commands. Child frontends leave the inbox to the group consumer rather than draining/re-enqueuing and losing provenance. Live input-form references inside entries are transient. Restore retains input without starting work. |
| `tool_counter`, `tool_results`, `tool_records`, `tool_errors`, `recent_commands` | Local durable receipts and bounded activity. Pruning one agent cannot delete another's records. Child reports enter a parent only through explicit tool results or bounded result events. |
| `source_view_counter`, `source_views` | Local durable evidence; values are immutable. Equal numeric view keys do not authorize edits using another agent's evidence. |
| `turn_diffs`, `history`, `usage`, `compaction_usage` | Local durable edits, compacted spans and request statistics. Compaction usage is separate from conversation usage and from every sibling. |
| `transcript_messages`, `transcript_tool_records`, `transcript_turn_diffs`, `transcript_incomplete` | Local durable visible history/replay metadata. Legacy tool records are a read-only replay bridge; live transcript does not aggregate siblings. |
| `subagent_entries`, `subagents` | The root alone maintains the durable child manifest. Every Session refers to the one runtime group; children do not own a second group or duplicate its manifest. |
| `mcp`, `skills`, `catalog` | Shared root-owned capabilities; their mutable state describes servers/auth, workspace skill discovery/trust, and provider catalog sync. It contains no per-agent turn state. |
| `mcp_resource_reads`, `skill_listing` | Local runtime knowledge. MCP document injection and skill announcements must reach each context separately, even when catalogs are shared. Reset/compaction invalidate this agent's knowledge epoch; resume rebuilds it. |
| `shell_hooks`, `mentions`, `agents` | Local runtime handles. Hooks detach the immediate parent's configuration at creation. File completion owns frontend callbacks; instruction references bind this Session. Sharing either resolver was an isolation bug. |
| `images`, `image_route`, `learned_text_only_routes` | Local runtime asset access and learned route evidence. Image references/files are durable; learned provider rejection evidence is rebuilt. |
| `jobs`, `job_counter` | Local runtime process registry; temporary log paths are randomized. Processes/output threads are settled by lifecycle shutdown. They are not recoverable snapshot state. |
| `quick_hints`, `next_hints_available`, `context_epoch` | Local runtime UI/context projection state. Context epochs invalidate only this model's frozen prefix. |
| `_active_turn_messages`, `_active_transcript_messages` | Local staging buffers. Their messages enter checkpoints; the live containers/handles do not. |
| `_snapshot_saved`, `_blobs_written`, `_meta_written` | Local write receipts/caches, initialized from this UID's log. Never reuse the parent's markers. |
| `_snapshot_gate`, `_snapshot_gate_loop`, `_snapshot_path` | Local write serialization and resolved destination. Image admission holds the same gate until publication and reference pinning finish, excluding snapshot GC. The gate is loop-bound and never shared across agent UIDs. |
| `_lease`, `_lease_borrowed`, `_ownership_released`, `_active_runs` | The capability is shared within the family; borrow/release/run accounting is local. A child cannot release its owner's lease. |

`AgentState` has durable `goal`, `plan`, `known`, `check`, `summary`, `name`, `name_source`,
`compaction_count`, `round_count`, `last_turn_status` and `last_turn_error`. The remaining fields
are local runtime state: `context_percent`, `context_tokens`, `turn_step`, `turn_messages`,
`current_model_call_started_at`, `manual_model_retry_requested`, `model_retry_count`,
`current_model_attempt`, `model_retry_reason`, `model_retry_until`, `awaiting_input`,
`stream_started_at` and `stream_chars`. None may drive a sibling's divider, statusbar or retry.

## Runtime objects and shared mutable state

| Boundary | Ownership |
| --- | --- |
| `Agent`, `ContextManager`, `ToolRunner`, `ModelClient` | Constructed per Session. Turn task/loop, source collection, hook redirects, compaction guards, runner capacity/gateways/active tool batch, vision client, model retry metadata and wire clients are local. One `UiHooks` instance binds the layers of one agent only. |
| Model request leases | A request installs its own ContextVar lease and resets it after settlement. Context inheritance by an asyncio task is not permission to reuse a parent's provider clients. Cache keys may share a workspace prefix; request messages and usage remain local. |
| `Subagents`, `AgentEntry`, `AgentCounts` | One group controls retained entries, root limit, admission lock and drivers. Each entry owns its child task and task description; displayed status derives from that child's Session/engine. Counts are a fresh immutable projection including main; input waits take precedence over running. Never persist counters or aggregate usage into them. A child exception or wait timeout cannot cancel a sibling. |
| `CommandLoop`, `TuiRuntime`, `Presentation`, `TuiApp`, `ScrollbackWriter` | One set per agent: draft/buffer, history/completion, submissions, pending reads, approvals/modals, transcript, stream/progress, output admission/drain and view state. A switch changes projection, not hooks or data ownership. |
| Keyboard history files | Main frontends read/append `<data_dir>/history.txt` for cross-session/project recall. Each child reads/appends only its UID's `.history` sidecar; it never reads or writes main's file. Each frontend owns its buffer/history loader. Recall is manual input editing, not conversation inheritance or automatic context injection. |
| `AgentsFrontend` | One selected-runtime pointer and runtime registry. Application-ready/shutdown events are shared intentionally. Notices name their source and stay out of model context. |
| `Theme` and formatting caches | Application-wide appearance/pure formatting state. A terminal has one theme; theme mutation does not imply shared agent statistics or messages. |
| MCP manager | Root configuration supplies connection settings and transport timeouts. Catalog/errors, discovery counters, OAuth store/login/refresh gates and operation cancellation scopes are service state. Cancellation settles the caller's operation; only root shutdown closes the manager after all engines settle. Document-injection `seen` is supplied by the caller. |
| Skill library and provider catalog | Workspace/data-dir discovery, project trust and installed/catalog facts are shared. Skill activation, hook configuration, frozen listing/announcements and chosen provider values remain local. Updates can become visible to every agent without copying their conversations. |
| File transaction locks | A process-wide weak registry keyed by normalized realpath coordinates Read snapshots and Edit validation/write, including symlink aliases. It contains no agent state, permits independent paths concurrently and retains no idle path history. |

Do not “optimize” by sharing FileMentions because cwd matches: its pending completion and
refresh-owner callback would route child events into the root and child close would disable root
completion. Likewise a shared MCP manager's “already injected” set suppresses required context
in other conversations. These are ownership problems, not callback-order problems.

## Files, diffs and persistence

Each UID has its own JSONL, metadata, assets and exported history paths. Child UIDs are
`root.uid + ".a" + random_id`; the family lease guards the root identity while separate snapshot
gates guard separate logs. Writes freeze a plan on the owning loop, execute I/O on a worker and
publish the receipt back to that Session. Blob hashes may match across logs without sharing
their write markers. Asset collection considers only captured files in that UID's directory;
it cannot prune another agent's output or an attachment accepted after the plan was frozen.
Latest pointers/listing and retention treat the root as the family entry point.

Workspace files are shared deliberately. Edit validation and write form one process-local
transaction. Two independently planned edits with the same baseline cannot both commit after
one changes it; the stale plan is refused. Direct edits re-read under the lock and can preserve
disjoint changes. Shell commands, other wizolt processes and external editors do not take these
locks. Task file boundaries and changed-target validation remain necessary; this is neither an
OS filesystem lock nor an isolated worktree. Hard links with different realpaths are likewise
outside the path-lock guarantee.

Diffs are receipts of **this agent's Edit calls**, not a Git diff of the shared workspace.
Continuous snapshots can be reduced to a net diff. If a peer/external edit breaks continuity,
retain this agent's individual receipts rather than subtracting its first snapshot from a later
shared file and attributing foreign changes to it. Snapshot-less tails derive from recorded
hunks, never today's file. Legacy records without snapshots use conservative reconstruction or
fall back to receipts. Rename inference requires an unambiguous continuous history.

Stopping a turn leaves its committed edits and background jobs available in the same session.
Archiving is different: it joins a descendant branch, marks its root-manifest entries archived,
and closes its frontend and engine resources. Logs and assets remain available to the picker's
read-only viewer; archived entries neither consume slots nor attach engines on resume.
Every archive entry point first drains frontend submissions outside the group's admission lock:
a cancelled turn may need its consumer to commit a FIFO boundary, while that consumer needs
admission to send between-turn input. The frontend admission context reopens retained views on
failure; a separate archive lock serializes this preparation and retirement sequence.
Application-session close stops owned jobs, joins promoted output threads, removes temporary
logs and closes clients. Resume reconstructs services, restores each child without running it,
and preserves its pinned provider/model/effort/API choices over the root's current configuration.
Do not persist the whole Config: secrets, machine paths and transient runtime controls have a
different lifetime from conversation data.

Restore holds the admission lock while loading and registering children, before a frontend starts
dispatching new turns. A child decode/I/O failure is isolated and reported; its manifest reference
and files remain intact. Healthy descendants still load with their own history and frozen model
choices; if their parent is unavailable, runtime hook inheritance falls back to main.

The inbox consumer retains ownership through its final snapshot. Input arriving in that await
must schedule another consumer after settlement, not overlap it. Failure and interruption pause
existing queued work; a fresh start request during settlement is explicit permission to resume.
`AgentEntry.restart_requested` is a transient wake request, never conversation state or a counter.

Turn timing and the bounded latest result belong to each `AgentState`; the monotonic live clock
is never persisted. `AgentEntry.result` publishes that result only after the child's save succeeds.
Delivery receipts belong to the receiving parent's `AgentState`, not to the group: independent
parents must not consume each other's notifications. Receipts and their session events enter the
same snapshot; they never enter user input history or the visible transcript. The archived
manifest retains the last result envelope after its engine is gone.
Successful, untruncated Subagent tool messages carry structured local receipt metadata; the
request boundary acknowledges only receipts actually present in the turn. Tool framing and hook
feedback are not a delivery protocol. Wire adapters strip this metadata, and transcript projection
omits it. Archive records use a boolean flag and a structured result envelope, whose bounded text
also supplies the preview; the full answer remains in the child's snapshot.

`agent/inspection.py` copies a bounded projection on the owning loop; it never lends callers
mutable state, credentials, hidden reasoning or raw image data. The runner owns its active
top-level batch and clears it on every exit. Archived inspection loads detached session values
without assembling an engine. Model inspection and frontend previews do not share UI objects.

## Regression boundaries

- `tests/test_subagents.py`: independent requests/config/usage, steering, sibling cancellation,
  approval drafts, root admission limits, nested hooks, family ownership and restore.
- `tests/test_subagent_state.py`: frontend resolver ownership, independent notes/plan/receipts/
  usage/inboxes/assets during concurrent save and restore, inherited model pinning, interleaved
  diff attribution and job lifetime/IDs.
- `tests/test_concurrent_edits.py`: overlapping workers and symlink aliases, stale planned-edit
  refusal, preserved direct edits and progress on unrelated files.
- `tests/test_mcp_resources.py`, `tests/test_skill_cache.py`: per-conversation document/skill
  knowledge with shared discovery.
- `tests/test_agents_ui.py` and real tmux/Zellij tests: accepted-input destination, local output,
  selected-agent status/projection, switching and shutdown.
- Session persistence/ownership and diff tests: UID-scoped artifacts, leases, save gates,
  mid-write attachments, pruning and conservative reconstruction.

When adding state, identify its owner, writer, readers, persistence and cleanup first; update
the inventory and add a behavior test at the boundary that could cross agent ownership.
