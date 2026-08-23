# Contributing

Before submitting a change:

```bash
ruff format --check .
ruff check .
pyright
lint-imports
pytest --cov=loopforge --cov-branch
```

Architecture changes should include or update an ADR in `docs/decisions/`.
