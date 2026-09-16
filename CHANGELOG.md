# Changelog

All notable user-facing changes are recorded here. LoopForge follows
[Semantic Versioning](https://semver.org/) for the installable package. The
PACS architecture milestones are an independent planning history and do not
imply a package-version number.

## [Unreleased]

## [0.2.1] - 2026-09-16

### Changed

- Clarified that LoopForge is primarily a usable reference architecture for
  deterministic, transparent, and governable agentic systems.
- Reconciled the completed PACS reference-architecture milestone with the
  package's public-beta version line.
- Updated the security documentation to describe the existing hardened
  container adapter and its limits.
- Restricted source distributions to the files required to build and install
  the package, excluding local caches and `node_modules`.
- Added tagged-release automation, artifact smoke tests, contribution
  templates, dependency updates, and release documentation.

## 0.2.0 - 2026-09-07

### Added

- Zero-configuration `loopforge console` installation path with a packaged
  React operator console and SQLite state.
- Live Ollama and DeepSeek adapters, deterministic routing, orchestration,
  provenance, locked benchmark evaluation, and adaptive policy experiments.
- Public documentation, Apache-2.0 licensing, contribution guidance, and
  private vulnerability reporting.

## 0.1.0 - 2026-09-07

### Added

- Initial public beta of the deterministic runtime kernel, durable event
  store, reliability controls, sandbox contracts, and typed context model.

[Unreleased]: https://github.com/Rsan0948/LoopForge/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Rsan0948/LoopForge/releases/tag/v0.2.1
