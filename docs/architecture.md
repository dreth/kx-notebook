# Architecture

`kx-notebook` separates q execution from portable notebook display. This keeps
direct IPC useful without PyKX and gives embedders a small evaluator boundary.

## Execution path

1. IPython registers the `%kx` management magic and `%%q` cell magic.
2. `%kx` selects/configures an evaluator for the current kernel process.
3. `%%q` passes q source plus execution limits to that evaluator.
4. The result adapter produces a table-shaped value or bounded q text.
5. The contract builder emits versioned KX JSON, escaped HTML, and plain text.
6. IPython publishes the MIME bundle as the cell's ordinary display output.

Connection state and runtime secrets live only in the kernel process. Persisted
notebook output contains the bounded result contract, not a reusable connection
or credential.

## Evaluator boundary

The synchronous evaluator abstraction accepts q source and returns a normalized
evaluation result. Implementations are:

- **Direct IPC:** owns a TCP socket, q handshake, synchronous request framing,
  response decoding, limits, timeout, and close behavior.
- **Callback:** delegates execution to a caller-owned synchronous callable.
- **PyKX:** lazily imports PyKX only after explicit selection and adapts its
  result into the common contract.
- **Broker HTTP:** posts to an explicitly supplied local broker URL with a
  bearer token resolved at runtime and validates the bounded response.

The broker adapter is deliberately client-only in 0.1.0. It neither starts nor
discovers `vscode-kdb`. A profile may name the loopback URL and token environment
variable, but never contains the token itself.

### Broker HTTP v1

The evaluator sends one `POST` to `{base_url}/v1/evaluate` with
`Authorization: Bearer <runtime-token>`, JSON content, and this request shape:

```json
{
  "version": 1,
  "source": "select from trade",
  "limits": {"rows": 20, "bytes": 1000000},
  "timeoutSeconds": 30.0
}
```

`timeoutSeconds` is omitted when the caller sets no query timeout. A table
response has `version: 1`, `kind: "table"`, unique string `columns`, rectangular
`rows`, a truthful `rowCount`, and an optional `label`. A text response has
`version: 1`, `kind: "qText"`, string `text`, and optional boolean `truncated`.
Unknown fields do not relax validation.

Both HTTP and HTTPS broker URLs are restricted to loopback. URLs containing
user information and all redirects are rejected, so the bearer token is not
forwarded to another authority. Response size, shape, and timeout are bounded
before display.

## Direct IPC trust boundary

The decoder treats all server bytes as untrusted. It validates frame size,
endianness, message kind, q type tags, collection sizes, nesting, and bounded
allocation before constructing Python values. Common lossless values become
typed table cells. An unsupported value becomes bounded `qText` only when a
safe textual representation exists; otherwise evaluation fails explicitly.

The request path is synchronous. Closing on timeout/cancel stops local I/O but
does not assert server-side cancellation. TLS is not part of the 0.1.0
transport. Response I/O, decompression, and decoding share one query deadline.
The operating system may not make an in-progress DNS lookup/TCP connect call
immediately interruptible.

Complete cells use the legacy-compatible `vscode-kdb` semantics: physical q
lines are grouped client-side and reduced through `value` while restoring the
previous namespace. The evaluator does not depend on `.Q.ld`, so it remains
usable with older q installations where that helper is absent.

The direct evaluator composes that complete-cell expression exactly once as the
argument to a server-side transport wrapper. The wrapper classifies the single
result, leaves non-tables unchanged, and applies `count` plus a bounded take to
keyed and unkeyed tables. It returns a four-field private envelope containing a
fixed marker, result kind, true table row count (or null for non-tables), and
the bounded value. Because every successful result is wrapped and the nested
payload is never recursively interpreted, user data that resembles the
envelope cannot collide with the protocol.

Envelope shape, marker bytes, field types, result kind, total count, keyedness,
preview count, row limit, and internal cell budget are validated before
redaction or display. The q wrapper also measures its serialized table envelope
and shrinks it below both the configured receive limit and an item-safe internal
wire budget. Over-wide schemas or previews that cannot fit even one row become
explicit bounded omissions. The decoder retains its established opaque-value
fallback inside an otherwise valid envelope.

Native q errors occur before envelope construction and retain the existing
restore-and-rethrow behavior. Assignments and other state changes therefore
occur once and persist exactly as before, including changes made before an
error. Redaction still happens after transport validation, and timeouts,
interrupts, and cancellation still close the same single synchronous request.

## Portable result contract

The MIME type is `application/vnd.kx.result+json`; version 1 matches the
existing `vscode-kdb` notebook contract.

A table payload has explicit schema, typed row cells, total and preview counts,
limits, truncation state/reasons, and bounded provenance. A q-text payload
contains bounded text plus the same truthful truncation accounting. HTML and
plain-text bodies are generated from the same bounded payload.

The byte limit covers all emitted MIME bodies. Result provenance does not
include credentials or a hidden live-result identifier. q source is included
only through an explicit safe API choice; normal `%%q` output need not duplicate
the source already stored in the cell.

Immediately before IPython display, the evaluator's runtime redactor scans the
canonical custom JSON plus complete HTML and plain-text fallbacks. This catches
credentials that could otherwise be reconstructed only when separate values
and serialization delimiters are joined. On a match, the complete bundle is
discarded and replaced with a content-free omission notice. If even the fixed
notice/contract text matches the credential, output is suppressed rather than
leaked through a fallback or exception traceback.

## Profiles

The configuration layer resolves an XDG/platform user config path, parses TOML
strictly, and stores non-secret direct or broker connection metadata. Password
resolution is ordered by explicit runtime input, a named environment variable,
then an explicitly selected optional system-keyring lookup. Broker tokens are
resolved from the configured environment-variable name. A missing optional
dependency produces an actionable error instead of importing keyring at package
startup. Configuration files are bounded regular files, but their parent
directory remains a local trust boundary: it should be owned and writable only
by the kernel account. The same ownership expectation applies to an IPython
profile selected for the optional startup hook.

## Provenance

The initial contract, display, magic, fallback, tests, and PyKX adapter are
ported/adapted from `python/kx_notebook` in the MIT-licensed
[`dreth/vscode-kdb`](https://github.com/dreth/vscode-kdb) repository. Direct IPC
wire behavior follows its tested `src/q-ipc.ts` implementation and related q
result types. This standalone repository adds its own package/configuration,
CLI, evaluator boundary, IPC transport, broker contract, and release process.

The port was audited against `vscode-kdb` commit
[`4353256d`](https://github.com/dreth/vscode-kdb/commit/4353256d6fc536607931cd6d49b93c05c7ced238).
Useful history starts with the portable notebook output in
[`2c9632a3`](https://github.com/dreth/vscode-kdb/commit/2c9632a3b832544fef735ea087098b5a905fe016),
the standalone connection/IPC lineage in
[`fd750d05`](https://github.com/dreth/vscode-kdb/commit/fd750d055168b3c193e36b65508294abf44e94c4),
and legacy-compatible multiline q evaluation in
[`7629d6f7`](https://github.com/dreth/vscode-kdb/commit/7629d6f7708f5db6296e2d06787d83b8c724f750).
