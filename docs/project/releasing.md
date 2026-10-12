# Releasing

Releases are published to PyPI by the `release.yml` workflow through PyPI trusted publishing. No
token is needed anywhere; the publisher for this repository, workflow `release.yml`, environment
`pypi`, is already registered on pypi.org.

## Steps

1. Bump `__version__` in `src/harnesslab/__init__.py`, `version` and `date-released` in
   `CITATION.cff`, and the BibTeX `version` on the landing page (`docs/index.md`);
   `tests/test_packaging.py` checks that they agree with the changelog.
2. Rename the `## Unreleased` heading in `CHANGELOG.md` to `## X.Y.Z (YYYY-MM-DD)`, and mirror it
   in `docs/project/changelog.md`.
3. Turn the docs' unreleased markers into release markers: the `> **Unreleased.** On main; ships
   in the next release.` banners and the `*(unreleased)*` tags become `> **New in Harness Lab
   X.Y.Z.**` and `*(X.Y.Z)*` (`grep -rn -i "unreleased" docs/` finds them all).
4. If the dashboard or the CLI output changed visibly, regenerate the site images
   ([Contributing](contributing.md#site-images)).
5. Verify:

   ```bash
   uv sync && uv run pytest -q && uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
   uv run mkdocs build --strict
   uv build && uv run twine check dist/*
   ```

6. Commit to `main`, wait for CI to pass on it, then tag and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "harnesslab X.Y.Z"
   git push origin vX.Y.Z
   ```

7. Watch the workflow: `gh run list --workflow=release.yml`. It runs the tests, builds the
   distributions, and publishes. Confirm at <https://pypi.org/project/harnesslab/>, then install
   it in a clean environment and run `harnesslab run demo --variants fake-reference,fake-noop`.

A version number can never be reused on PyPI; if a release fails after upload, bump again.

## Documentation

The docs deploy to GitHub Pages from `main` on every push by the `docs-deploy` job in `ci.yml`, so a
release needs no separate documentation step. The site's front page (`docs/index.md`) is a landing page that
only MkDocs shows; GitBook opens on Start here → Overview. A GitBook space connected to the repository syncs
from the same `docs/` folder.
