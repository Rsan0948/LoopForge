## Summary

Describe the behavior or contract changed and why.

## Authority and risk

- [ ] This change does not expand runtime authority, or the expansion is explicitly documented and reviewed.
- [ ] Side effects, retry/idempotency behavior, budgets, and HITL implications are addressed.
- [ ] Model- or repository-controlled content cannot weaken code-owned controls.

## Evidence

- [ ] Allowed behavior is tested.
- [ ] Denial or failure behavior is tested where a control rule changes.
- [ ] `ruff format --check .`, `ruff check .`, `pyright`, `lint-imports`, and `pytest --cov` pass.
- [ ] UI typecheck/build pass when applicable.
- [ ] Documentation or an ADR is updated when the architecture contract changes.
