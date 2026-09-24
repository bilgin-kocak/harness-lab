# Releasing

Releases are published to PyPI by the `release.yml` workflow through PyPI trusted publishing. No
token is needed anywhere; the publisher for this repository, workflow `release.yml`, environment
`pypi`, is already registered on pypi.org.

## Steps

1. Bump `__version__` in `src/harnesslab/__init__.py`.
2. Replace `(unreleased)` in `CHANGELOG.md` with the date, and mirror it in
   `docs/project/changelog.md`.
3. Verify:

   ```bash
   uv sync && uv run pytest -q && uv run ruff check src tests
   uv run mkdocs build --strict
   uv build && uv run twine check dist/*
   ```

4. Commit to `main`, then tag and push the tag:

   ```bash
   git tag -a v0.2.0 -m "harnesslab 0.2.0"
   git push origin main v0.2.0
   ```

5. Watch the workflow: `gh run list --workflow=release.yml`. It runs the tests, builds the
   distributions, and publishes. Confirm at <https://pypi.org/project/harnesslab/>.

A version number can never be reused on PyPI; if a release fails after upload, bump again.

## Documentation

The docs deploy to GitHub Pages from `main` on every push by the `docs` job in `ci.yml`, so a
release needs no separate documentation step. A GitBook space connected to the repository syncs
from the same `docs/` folder.
