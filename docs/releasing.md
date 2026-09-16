# Releasing LoopForge

LoopForge uses Semantic Versioning for the installable `loopforge-console`
package. PACS architecture milestones are a separate historical record.

## One-time repository setup

1. In PyPI, add a trusted publisher for repository `Rsan0948/LoopForge`,
   workflow `release.yml`, environment `pypi`, package `loopforge-console`.
2. In GitHub, create the `pypi` environment. Requiring a maintainer approval is
   recommended so a tag cannot publish without a final human check.
3. Protect `main` and require the `quality` and `ui` CI jobs.

No long-lived PyPI token is required or expected.

## Release checklist

1. Update `pyproject.toml`, `ui/package.json`, `ui/package-lock.json`,
   `CITATION.cff`, and `CHANGELOG.md` to the same package version.
2. Build the UI and artifacts:

   ```bash
   cd ui && npm ci && npm run build && cd ..
   uv build --clear
   ```

3. Smoke-test both artifacts in clean environments:

   ```bash
   uv run python scripts/smoke_artifacts.py \
     --expected-version 0.2.1 \
     dist/loopforge_console-0.2.1-py3-none-any.whl \
     dist/loopforge_console-0.2.1.tar.gz
   ```

4. Confirm the source archive contains no caches, virtual environments, or
   `node_modules`.
5. Merge the release commit only after CI passes.
6. Create and push the signed release tag:

   ```bash
   git tag -s v0.2.1 -m "LoopForge v0.2.1"
   git push origin v0.2.1
   ```

The tag-triggered workflow rejects a tag that does not match
`project.version`, rebuilds and smoke-tests both distributions, publishes to
PyPI using trusted publishing, and creates a GitHub Release with the artifacts.

## Recovery

Published PyPI versions are immutable. If metadata or artifacts are wrong, do
not attempt to replace them; correct the problem and issue the next patch
version. A GitHub Release may be marked as a prerelease or have its notes
amended, but its tag must continue to identify the exact published source.
