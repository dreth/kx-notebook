# Releasing to PyPI

`kx-notebook` publishes through GitHub Actions and PyPI Trusted Publishing. Do
not create a PyPI API token or repository secret.

## One-time setup

1. In GitHub, optionally create an environment named `pypi` under
   **Settings → Environments**. The workflow can create it on first use, but
   creating it explicitly lets maintainers add deployment protection rules.
2. In the PyPI account's **Publishing** page, add a pending GitHub publisher
   with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `kx-notebook` |
   | GitHub owner | `dreth` |
   | GitHub repository | `kx-notebook` |
   | Workflow filename | `publish-pypi.yml` |
   | Environment name | `pypi` |

A pending publisher does not reserve the project name until the first publish.

## Publish a release

1. Update `project.version` in `pyproject.toml`, `kx_notebook.__version__`, and
   `CHANGELOG.md` in one commit.
2. Ensure the `main` CI workflow is green for that commit.
3. Create a GitHub release targeting that commit with tag `vX.Y.Z`. The tag
   must exactly match the package version; for 0.1.0 use `v0.1.0`.
4. Publishing the GitHub release triggers `publish-pypi.yml`. It validates the
   tag, runs the package tests, builds the wheel and sdist, checks both with
   Twine, and exchanges GitHub's OIDC identity for a short-lived PyPI token.
5. Verify the release at <https://pypi.org/project/kx-notebook/> and from a
   clean environment:

   ```bash
   uvx --from kx-notebook kx-notebook --version
   ```

PyPI files and versions are immutable. If a release fails after any artifact
was accepted, increment the version; never replace an uploaded distribution.
