# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.1.0] - 2026-07-27

### Added

- Standalone Python 3.9–3.13 package and `kx-notebook` console command.
- IPython extension with `%%q` execution and `%kx` connection/profile
  management.
- Direct q IPC evaluator with authentication, timeouts, bounded decoding, and
  safe fallback text for unsupported values.
- Explicit callback, optional PyKX, and authenticated local broker HTTP
  evaluators.
- Strict non-secret TOML profiles with runtime environment/keyring password
  resolution.
- Fail-closed credential checks over canonical JSON, HTML, and plain-text
  output after complete serialization.
- Version 1 `application/vnd.kx.result+json` table and q-text output, plus
  self-contained HTML and plain-text fallbacks.
- Idempotent, profile-scoped IPython startup hook management.
- Unit, IPython, notebook, deterministic IPC, broker, and local live-q tests
  that skip when q is unavailable.
- Python-version CI and a release-only PyPI trusted-publishing workflow.

[Unreleased]: https://github.com/dreth/kx-notebook/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dreth/kx-notebook/releases/tag/v0.1.0
