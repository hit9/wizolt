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
