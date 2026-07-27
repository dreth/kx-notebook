# Contributing

Contributions are welcome. Keep changes small, typed, tested, and free of
credentials or proprietary q data.

## Development setup

Install [uv](https://docs.astral.sh/uv/), clone the repository, then create the
locked development environment:

```sh
uv sync --all-extras --group dev --locked
```

The PyKX extra downloads separately distributed KX software and is not required
by the direct IPC implementation. If it is unsuitable for your environment,
use `uv sync --extra keyring --group dev --locked` for ordinary development and
leave PyKX adapter tests skipped.

## Checks

Run the same core checks as CI:

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
uv run twine check --strict dist/*
```

Formatting changes can be applied with `uv run ruff format .`.

Tests that use protocol fixtures must be deterministic and network-free. Live-q
tests must bind only an unused loopback port, skip clearly when q is unavailable,
and always stop the child process. Never assume `.Q.ld` exists; preserve
legacy-compatible query semantics.

For a package smoke test, install the wheel rather than the source tree into a
fresh temporary environment:

```sh
uv venv /tmp/kx-notebook-wheel-smoke
uv pip install --python /tmp/kx-notebook-wheel-smoke/bin/python dist/*.whl
/tmp/kx-notebook-wheel-smoke/bin/python -c \
  "import kx_notebook; print(kx_notebook.__version__)"
/tmp/kx-notebook-wheel-smoke/bin/kx-notebook --version
```

Choose a different explicit temporary path if that path already contains data.

## Compatibility and security

- Keep runtime support at Python 3.9–3.13 for the 0.1 series.
- Do not add a required PyKX dependency.
- Do not persist passwords or bearer tokens, include them in exceptions, or
  place them in MIME output/notebook metadata.
- Bound data before serialization. A preview must disclose truncation and must
  never claim to be the full result.
- HTML fallbacks must escape untrusted content and load no remote assets.
- Treat q IPC input as hostile bytes: validate lengths, types, nesting, and
  allocation bounds before decoding.

Report a suspected vulnerability privately to the repository owner before
opening a public issue containing exploit details or credentials.

## Source history

The initial portable contract, display, magic, fallback, and PyKX adapter work
is adapted from `python/kx_notebook` in
[dreth/vscode-kdb](https://github.com/dreth/vscode-kdb). Direct IPC behavior is
based on the tested TypeScript implementation in `src/q-ipc.ts` and related q
result types in that repository. The audited source snapshot and origin commits
are recorded in [Architecture](docs/architecture.md#provenance). Preserve useful
attribution in module docstrings and commit history when porting further code.

Do not copy code from sources whose license is incompatible with this
repository's MIT license.

## Pull requests and releases

Explain behavior changes, tests, result-contract compatibility, and any security
impact. Generated environments, caches, distributions, credentials, and
`*.egg-info` do not belong in commits.

Maintainers publish from a GitHub release whose `vX.Y.Z` tag exactly matches
`pyproject.toml`. The `pypi` environment uses OIDC trusted publishing; no PyPI
API token belongs in repository secrets or workflow files.
