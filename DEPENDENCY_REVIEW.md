# Dependency review

Reviewed on 2026-09-27 against `ccb16d3` (wizolt 0.55.1).

## Conclusion

No production dependency was identified as safe to remove without replacing behavior or changing
the installation contract. The strongest candidate for replacement is the pair of model SDKs,
`openai` and `anthropic`. Making MCP optional is a separate opportunity to reduce the installation
footprint for users who do not use it. Keep the terminal and rendering dependencies for now.

This is an investigation, not an implementation plan or a measured performance improvement.
No dependencies have been removed.

## Measurement scope

The review inspected `pyproject.toml`, source imports and call sites, installed package metadata,
and fresh-interpreter import behavior in the current Linux ARM64 / CPython 3.14.7 environment.

- There are **9 direct production dependencies** and **40 packages in their resolved runtime
  dependency closure**, occupying approximately **46.81 MiB**.
- Sizes sum installed distribution files, excluding `__pycache__`. They exclude wizolt itself
  and development-only dependencies. They are not wheel download sizes, process memory, or
  startup-time measurements.
- Removal estimates recompute the dependency closure after removing the named direct dependency,
  using installed versions and dependency markers for this environment. Shared dependencies stay
  counted. These are hypothetical savings for a fresh production installation; nothing was
  uninstalled from the development environment.
- Platforms, versions and enabled extras can change these numbers. Savings assume any replacement
  reuses the remaining dependencies; they exclude replacement code and new packages it might need.

The installed direct dependencies were `anthropic 1.0.0`, `anyio 4.14.2`, `mcp 2.2.0`,
`httpx2 2.12.0`, `openai 3.3.1`, `prompt-toolkit 3.0.53`, `pygments 2.21.0`, `rich 15.0.0`,
and `socksio 1.0.0`.

## Candidates and trade-offs

| Dependency | Assessment | Potential reduction | Required work or lost behavior |
| --- | --- | --- | --- |
| `openai` + `anthropic` | Best replacement candidate to investigate | 5 packages, about 10.65 MiB | HTTP requests, SSE decoding, response conversion, error classification and model discovery must preserve current behavior. |
| `mcp` | Prefer optional installation over a replacement client | 17 packages, about 19.09 MiB for installations without MCP | Guard feature entry points, explain missing optional support, and test both installation modes. |
| `rich` | Replaceable, but low priority | 3 packages, about 1.44 MiB | Replace Markdown, tables, wrapping and styled output while preserving terminal behavior. |
| `pygments` | Keep | Removing only its direct declaration saves nothing | Rich still requires it; wizolt also uses it directly for code and diff highlighting. |
| `prompt-toolkit` | Keep | Not evaluated as an isolated removal | It supplies the input, layout, completion and terminal-interaction foundation. |
| `anyio`, `httpx2` | Keep | Removing only their direct declarations saves nothing | Both are directly used by wizolt and required by other runtime dependencies. |
| `socksio` | Keep | 1 package, about 0.03 MiB | Removing it loses SOCKS proxy support for negligible savings. |

Removing Rich and Pygments together would release four packages totaling approximately 5.85 MiB,
but requires replacing both rendering and syntax highlighting. That work is substantially broader
than deleting package declarations.

## Model SDK replacement

The existing boundary makes this feasible:

- `wizolt/model/client.py` constructs both SDK clients with `max_retries=0`.
- The per-wire adapters already own request parameters, history conversion and stream-result
  reconstruction. The shared client owns accounting, request lifecycle and retry orchestration.
- `httpx2` is already a direct dependency and would remain available for transport.

The SDKs still perform meaningful work. A replacement must handle SSE framing and decoding,
request-field and header semantics, response shapes, status/transport errors, timeout and
cancellation behavior, and connection cleanup. `wizolt/model/resilience.py` currently recognizes
both SDKs' exception classes and extracts retry headers from them. Model discovery in
`wizolt/ui/cli/commands.py` also uses the OpenAI client.

Removing OpenAI alone would save about 6.28 MiB. Removing Anthropic alone would save about
3.51 MiB, including `docstring-parser`. Removing both also releases `jiter` and `sniffio`, bringing
the combined estimate to 10.65 MiB. **Pydantic remains required by MCP** and is not included in
these savings.

Validate a replacement through the existing wire-level mock-server tests, including streaming,
tool calls, provider-specific request fields, accounting, retry decisions, cancellation and
incomplete responses. Compare startup and request-processing benchmarks before adopting it;
smaller installation size alone does not establish a speed improvement.

## MCP installation scope

The official SDK serves both clients and servers. Its installed dependency closure includes
`starlette`, `uvicorn` and `python-multipart`, even though wizolt uses the client. A fresh-interpreter
probe of `import mcp.client` also loaded `starlette` and `uvicorn` in this environment.

The 17 packages exclusive to the MCP dependency path were:

`attrs`, `cffi`, `click`, `cryptography`, `jsonschema`, `jsonschema-specifications`, `mcp`,
`mcp-types`, `opentelemetry-api`, `pycparser`, `pyjwt`, `python-multipart`, `referencing`,
`rpds-py`, `sse-starlette`, `starlette`, and `uvicorn`.

This does not mean those packages are individually safe to delete while keeping the SDK. Some
support client behavior, including authentication and validation. The 19.09 MiB estimate applies
only when the MCP dependency is absent from the production installation.

An optional MCP extra would preserve the SDK for users who need it while allowing a smaller base
installation. It changes the installation contract and requires auditing startup warm-up,
feature assembly, commands, token handling and missing-package errors. Importing SDKs lazily
already helps startup, but does not reduce what a normal installation downloads.

Replacing the SDK is a larger undertaking: wizolt would own stdio, Streamable HTTP and legacy SSE
transport behavior, protocol/session handling, OAuth discovery and token refresh, and cancellation
cleanup. Prefer optional packaging before considering that maintenance burden.

## Dependencies to retain

- **Rich and prompt-toolkit have distinct roles.** Rich formats Markdown and other output;
  prompt-toolkit owns terminal interaction. Their coexistence is not evidence of duplication.
- **Pygments is directly used.** Removing its declaration while relying on Rich to install it
  would hide a real dependency rather than remove one.
- **AnyIO and HTTPX2 are directly used.** MCP cancellation uses AnyIO scopes; update checks,
  catalog refresh and MCP HTTP/auth paths use HTTPX2. Keeping their declarations is appropriate.
- **SOCKSIO is used indirectly.** HTTPX2 imports it when selecting a SOCKS transport and reports
  an error when it is missing. A source-import search alone would incorrectly mark it unused.

`Pillow`, `pathspec` and `json-repair` are already development-only dependencies. They provide
image fixtures and reference implementations for the project's image-header, ignore-file and
JSON-repair tests. Removing them would weaken validation without shrinking ordinary production
installations. The remaining development dependencies support tests, linting and type checking.

## Suggested order

1. Prototype replacement of the model SDK transport boundary, preserving the three wire adapters.
   Adopt it only after behavior tests and comparable benchmarks establish the benefit and cost.
2. Consider optional MCP installation if a smaller base installation is a product priority.
3. Keep the rendering and terminal stacks until profiling or a concrete feature requirement
   justifies taking over their responsibilities.

## References

- Local declarations: [pyproject.toml](pyproject.toml).
- Existing performance methodology: [benchmarks/README.md](benchmarks/README.md).
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).
- [MCP client transports](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md).
- [MCP OAuth clients](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md).
- [OpenAI SDK request, retry and SSE implementation](https://github.com/openai/openai-python/blob/main/src/openai/_base_client.py).

Upstream links describe responsibilities and may evolve. Version-specific package counts and
sizes above come from the local environment inspected for this review.
