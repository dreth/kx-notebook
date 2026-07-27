# kx-notebook

`kx-notebook` lets a normal Python/IPython/Jupyter kernel execute q and publish
portable, bounded notebook output. Direct q IPC is built in; PyKX is optional.

The rich result MIME type is `application/vnd.kx.result+json`, matching the
contract already consumed by
[dreth/vscode-kdb](https://github.com/dreth/vscode-kdb). Every rich result also
has escaped `text/html` and `text/plain` fallbacks, so saved notebooks remain
readable without a custom renderer.

This is the source for version 0.1.0. The installation below intentionally uses
a source checkout; do not assume PyPI availability until a release is
published.

## Install from a checkout

Python 3.9 through 3.13 is supported. Install into the same environment as the
IPython or Jupyter kernel that will run q cells:

```sh
git clone https://github.com/dreth/kx-notebook.git
cd kx-notebook

# uv
uv venv
uv pip install .

# or pip in an already activated environment
python -m pip install .
```

PyKX and keyring integrations are opt-in:

```sh
uv pip install '.[pykx]'
uv pip install '.[keyring]'
```

The direct IPC path does not import or require PyKX. KX q itself is not bundled;
you need access to a q process and must comply with the applicable KX license.

## Five-minute quickstart

Start a local q process on port 5000 in one terminal:

```sh
q -p 5000
```

Then start IPython or a Python-backed Jupyter kernel:

```python
%load_ext kx_notebook
%kx connect localhost:5000
```

Run a cell through the ordinary Run/Shift+Enter lifecycle:

```q
%%q
([] sym:`AAPL`MSFT; price:224.1 442.8)
```

Useful management commands are:

```python
%kx status
%kx profiles
%kx use PROFILE
%kx disconnect
%kx help
```

Per-cell overrides are explicit:

```q
%%q --profile local --max-rows 100 --max-bytes 524288 --timeout 5 --label "quote preview"
select from quote
```

The profile selects the evaluator for that cell; the label is display metadata.
Credentials are never copied into the result.

Before display, all three fully serialized MIME representations are checked
against the active direct password or broker token. If serialization itself
would reconstruct a runtime credential across values or schema boundaries, the
entire output is discarded and replaced with a content-free omission notice.
If even fixed contract text matches the credential, display is suppressed.
The `%%q` lifecycle wires this automatically; an embedder calling
`display_result` directly should pass its evaluator's `redact_text` method.

## Direct IPC

Direct IPC performs the q authentication handshake and synchronous request over
a TCP socket. It decodes common q atoms, vectors, dictionaries, keyed and
unkeyed tables, temporal values, symbols, strings, and nulls, and surfaces q
errors normally. Values outside the supported decoder are represented as
bounded q text when a safe representation is available; the package does not
invent a Python value.

Timeout or local cancellation closes the client socket. That stops waiting in
the notebook, but it cannot guarantee that an already-running server-side q
calculation was interrupted. One connection handles one synchronous request at
a time; requests are not multiplexed. The timeout covers response I/O,
decompression, and cooperative decoding. An operating-system DNS lookup or TCP
connect call cannot always be interrupted immediately by another thread.

TLS transport is not implemented in 0.1.0. Use a trusted local/private network
or a separately managed secure tunnel; do not expose an unauthenticated q port
to an untrusted network.

## Profiles and credentials

Profiles are strict TOML records stored in the platform-appropriate user config
directory. They contain kind-specific connection metadata and secret lookup
names only. Unknown fields and invalid types are rejected.

Find, validate, and list the effective configuration with:

```sh
kx-notebook config path
kx-notebook config validate
kx-notebook config profiles
```

`KX_NOTEBOOK_CONFIG` may point at a different TOML file for an isolated
environment.

A direct profile looks like this:

```toml
default_profile = "local"

[profiles.local]
kind = "direct"
host = "127.0.0.1"
port = 5000
username = "analyst"
password_env = "KX_NOTEBOOK_LOCAL_PASSWORD"
connect_timeout = 5.0
query_timeout = 30.0
max_receive_bytes = 67108864
```

`username`, `password_env`, and timeout fields are optional. `kind` must match
an implemented evaluator; TLS fields are rejected because 0.1.0 does not
implement TLS. `max_receive_bytes` may lower the direct decoder cap; 64 MiB is
the package maximum.

Do not put a password, broker bearer token, or other secret in the TOML file.
Passwords can be supplied to the Python API for the current process, resolved
through the profile's named environment variable, or read from a system
keyring when the `keyring` extra is installed. Runtime secrets are never
written back to the profile file.

For a one-off direct connection, the equivalent magic is:

```python
%kx connect localhost:5000 --username analyst --password-env KX_NOTEBOOK_LOCAL_PASSWORD --connect-timeout 5 --query-timeout 30
```

Avoid entering a password directly in a notebook cell: notebook source and
shell/IPython history are outside this package's output redaction boundary.
Prefer a named environment variable, a system keyring, or a secure host
application that passes an explicit runtime value.

## Evaluator modes

Direct IPC is the default standalone evaluator. Three explicit alternatives are
available:

- **Callback:** embed `kx-notebook` around a synchronous application-owned
  evaluator. The callback owns execution; `kx-notebook` owns bounded display.
- **PyKX:** install the `pykx` extra and select the adapter explicitly. PyKX is
  imported only then, and its own licensing/configuration requirements apply.
  Tables/keyed tables are bounded before Python conversion; vectors and
  dictionaries use bounded q-text previews.
- **Local broker HTTP:** supply the broker URL and bearer token at runtime. The
  adapter defines an authenticated request/response boundary for future
  `vscode-kdb` integration; it does not discover a broker or persist its token.
  Version 0.1.0 accepts loopback broker URLs only and rejects redirects and
  ambient HTTP proxies.

Configure an application-owned callback directly:

```python
from kx_notebook import configure_evaluator

configure_evaluator(my_synchronous_q_callback, label="application q session")
```

Select PyKX explicitly (this is the point where the optional dependency may be
imported):

```python
from kx_notebook import configure_evaluator
from kx_notebook.evaluators import PyKXEvaluator

configure_evaluator(PyKXEvaluator())
```

Or construct the broker adapter with runtime values:

```python
import os

from kx_notebook import configure_evaluator
from kx_notebook.evaluators import BrokerEvaluator

configure_evaluator(
    BrokerEvaluator(
        os.environ["KX_NOTEBOOK_BROKER_URL"],
        os.environ["KX_NOTEBOOK_BROKER_TOKEN"],
    )
)
```

A broker profile may persist only non-secret lookup metadata:

```toml
[profiles.local_broker]
kind = "broker"
base_url = "http://127.0.0.1:8765"
token_env = "KX_NOTEBOOK_BROKER_TOKEN"
timeout = 10.0
```

The bearer token is read from the named environment variable at runtime. The
package does not bundle or launch a broker.

See
[Architecture](https://github.com/dreth/kx-notebook/blob/main/docs/architecture.md)
for trust boundaries and the evaluator contract.

## Optional IPython startup hook

Loading the extension in a notebook is explicit and requires no installation
hook. If you want one IPython profile to load it automatically, inspect the
planned change first:

```sh
kx-notebook install --profile default --dry-run
kx-notebook install --profile default
kx-notebook status --profile default
kx-notebook uninstall --profile default --dry-run
kx-notebook uninstall --profile default
```

Install and uninstall are idempotent and affect only the selected IPython
profile. `kx-notebook` never silently modifies every Python or Jupyter
environment.

## Portable result contract

Contract version 1 persists either a table preview or bounded `qText`.
Table output records the declared total row count, persisted preview row count,
row/byte limits, and truncation reasons. A preview is never described as the
full result. Cell values use explicit portable kinds including temporal,
bigint, null, and JSON; symbols are losslessly displayed as string cells.

The package emits:

- `application/vnd.kx.result+json` for a compatible renderer;
- self-contained, escaped `text/html` with no network-loaded assets; and
- `text/plain` for terminals and basic notebook viewers.

Default output is limited to a 20-row preview and 1,000,000 total MIME bytes.
Raise limits only for results that are safe to embed permanently in a notebook.
A notebook file is not a secret store.

Static chart metadata can accompany compatible table results. Renderers may
offer richer interaction, but saved output contains only the bounded preview,
not a hidden live-result handle.

## Jupyter and Dev Containers

Install the package into the environment backing the selected kernel, then use
`%load_ext kx_notebook`. Installing only the console command in another virtual
environment will not make the extension importable by that kernel.

No repository-specific Dev Container is shipped in 0.1.0. In an existing Dev
Container, install Python 3.9–3.13 plus `uv`, run
`uv sync --all-extras --group dev --locked`,
and ensure the container can reach the intended q host. Keep credentials in the
container's secret/environment facility, not in the image or repository.

## Troubleshooting

**`ModuleNotFoundError: kx_notebook`**

The notebook is using a different Python environment. In the notebook, inspect
`sys.executable`, then install `kx-notebook` into that interpreter's environment.

**`%kx connect` cannot reach q**

Check host/port, q's `-p` setting, container/VM networking, and authentication.
The direct connection is TCP, not HTTP.

**A q query timed out**

The local socket is closed. Treat the server-side calculation as potentially
still running and inspect q before retrying an expensive query.

**A value appears as `qText`**

The IPC decoder did not have a lossless portable representation for that value,
or a configured output bound was reached. The bounded text is intentional.

**Keyring lookup fails in a headless container**

Install and configure a supported OS keyring backend, or use a named environment
variable for that runtime instead.

## Current limitations

- Direct IPC is synchronous and supports a documented subset of q IPC values.
- TLS, asynchronous q messages, and server-side query cancellation are not
  implemented.
- Local cancellation cannot preempt every operating-system DNS/connect call;
  connect deadlines are still enforced when that call returns.
- Persisted results are bounded previews; there is no promise that a full result
  can be reopened after the source session ends.
- The HTTP broker adapter is a client-side contract, not a bundled broker or
  automatic `vscode-kdb` bridge.
- PyKX is optional, separately distributed, and never used implicitly.
- Configuration and IPython profile directories are local trust boundaries:
  keep them owned and writable only by the account running the kernel. Profile
  metadata can select an endpoint and name a credential environment variable.

Development and release checks are in
[CONTRIBUTING.md](https://github.com/dreth/kx-notebook/blob/main/CONTRIBUTING.md).

## License

MIT. See [LICENSE](https://github.com/dreth/kx-notebook/blob/main/LICENSE).
