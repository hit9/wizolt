# Known Issues

Unresolved problems that look like bugs and are not going to be fixed by trying harder at the
same level. Each entry says what the symptom is, why it is inherent, and what evidence exists
that the alternative is not better. `DESIGN.md` holds the decisions; this file holds the costs
those decisions leave behind.

## Terminal resize costs on the primary screen

Wizolt keeps its whole UI on the terminal's primary screen so that completed output lands in
native terminal and tmux scrollback. Three symptoms follow from that choice. They are real, they
are reported, and the reference implementation of the same design has all three.

### 1. A width change clears the terminal, including pre-wizolt shell history

Changing the pane width rewraps every row, after which nothing can say which rows belong to the
application. The terminal is purged and the transcript re-emitted from memory, which takes the
shell output that was on screen before wizolt started with it. Users see this as wizolt jumping
to the top of the pane, because everything above it is gone rather than because wizolt moved.

**Not fixable by repainting less.** Redrawing only the rows above the app was implemented and
measured. The prototype's `physical_rows` and `wrap_rows` matched tmux's wrapping in the cases
measured at 20/40/60/80/100 columns, and in a quiet pane it
works, preserving shell history across every width change. It still fails, because a repaint can
only rewrite the visible screen while transcript rows that already scrolled into native history
stay as tmux reflowed them. The two versions disagree at the seam and the acceptance suite loses
and duplicates markers. See `DESIGN.md` for the full record; do not restart from the row
arithmetic, which is not where the problem is.

**codex does the same thing.** Its resize replay clears scrollback with the same escape sequence:

```
codex-rs/tui/src/app/resize_reflow.rs   clear_terminal_for_resize_replay -> clear_scrollback_and_visible_screen_ansi
codex-rs/tui/src/custom_terminal.rs     write!(self.backend, "\x1b[r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H")
```

`3J` purges scrollback. Its own reports of the consequence: [#35335 Windows loses scrollback after
long responses](https://github.com/openai/codex/issues/35335), [#11847 inline mode scrollback
truncated / jumps to pre-Codex history](https://github.com/openai/codex/issues/11847). Note that
the description of [PR #18575](https://github.com/openai/codex/pull/18575) reads as though external
content is preserved; the code says otherwise, and the code is what was checked.

### 2. A tall selector pushes context off the top and cannot bring it back

Opening an inline selector makes the application taller. On a pane with no spare rows the content
above is pushed into history, and closing the selector cannot pull it back, because the terminal
has no way to return rows from its own scrollback. In a pane short enough that the selector needs
the whole screen, context disappears rather than merely shifting.

Restoring it would mean repainting the freed rows from the transcript, which expands the
width-change cost onto every ordinary selector interaction. `DESIGN.md` records the decision not
to.

**codex has the broader form of this**, and shipped an escape hatch rather than a fix:
[#10331 Zellij scrollback still broken with `--no-alt-screen`](https://github.com/openai/codex/issues/10331),
[#14277 `--no-alt-screen` does not preserve scrollback in xterm.js terminals](https://github.com/openai/codex/issues/14277),
[#20063 iTerm2 + zsh: `--no-alt-screen` still does not produce usable native scrollback](https://github.com/openai/codex/issues/20063),
and [PR #20819](https://github.com/openai/codex/pull/20819) adding a raw scrollback mode.

### 3. The app moves down when the transcript is taller than the space above it

A rebuild keeps the application at the row it was on when there is room, and pads above the
transcript rather than below it. When the transcript needs more rows than that, the application
lands below it instead. Pinning it would mean writing only the rows that fit, which costs
scrollback depth -- the older rows would no longer reach native history at all.

### What would actually remove all three

Moving the whole application to the alternate screen and serving conversation history from inside
wizolt. That is a product decision, not a rendering fix: it gives up native scrollback, so
`Ctrl-b [`, terminal search and cross-pane copy stop reaching the transcript. It has been
considered and rejected. codex's `--no-alt-screen` issues above are what the other side of that
trade looks like in practice.

### What is not on this list

Content laid out for the wrong width. Rules, messages, tables, lists, code and diffs are recorded
as what they are and laid out again for the pane they are projected into (`WidthDependent` in
`wizolt/render.py`). codex reached the same place from the same starting point --
[#5259 Rerender scrollback after terminal resize](https://github.com/openai/codex/issues/5259),
fixed by [PR #18575](https://github.com/openai/codex/pull/18575) -- with a row cap and, by its own
description, "noticeable streaming lag for very long threads upon resize"; wizolt's replay bound
predates the change and is not a resize cost.
