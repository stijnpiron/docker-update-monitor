---
applyTo: "**"
---

# Update Monitor - Project Instructions

## Python Environment

This project has a virtual environment at `.venv/`. Always use it:

```bash
.venv/bin/python -m pytest tests/       # run tests
.venv/bin/python monitor.py             # run the monitor
```

Do NOT use `python`, `python3`, or `pip install` directly. The venv has all dependencies already installed.

## Running Tests

```bash
.venv/bin/python -m pytest tests/ -v
```
