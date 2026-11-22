# Render cleanup measurements

Baseline `97de1ceb`, Linux aarch64, CPython 3.14.7. Both versions used the same project
interpreter and dependencies. Timing samples ran sequentially, before tests and docs builds.

## Theme preview

Change identified by generator blob `1055a89160` and widget blob `5ea5af4cf5`.
Ran `.venv/bin/python docs/tools/theme_preview.py --fragment --output /tmp/preview.html`
in a fresh process per sample, with one warm-up. The median of five wall-clock samples fell
from **1.274 s to 0.782 s**. Uncompressed fragment size fell from **1,671,759 to 843,621 bytes**.
This measures preview generation only. For all 12 themes, both sides of every comparison,
including every status bar and divider SVG, matched the baseline byte for byte.

| Sample (ms) | 1 | 2 | 3 | 4 | 5 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before | 1274 | 1303 | 1208 | 1184 | 1358 |
| After | 816 | 779 | 785 | 782 | 767 |

## Status Context tab

Status renderer blob `089880e489`, compared with the baseline above. Using the
`status_loop` fixture from `tests/test_loop_skills.py`, collected the snapshot once and
timed `StatusTabs(snapshot).context(76)` on the same instance, after one warm-up.
Across seven batches of 400 draws, median time per draw went from **70.95 to 64.82 µs**.
Styled fragments matched exactly (SHA-256 of their Python representation
`0b1d779e4346d3885509d734c5aebc344d85f22e7d12d7f2adad2fb2255d8490`).

| Per draw (µs) | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Before | 69.69 | 71.06 | 73.47 | 70.99 | 69.07 | 70.95 | 70.45 |
| After | 63.70 | 61.99 | 66.89 | 64.82 | 65.13 | 65.17 | 63.80 |
