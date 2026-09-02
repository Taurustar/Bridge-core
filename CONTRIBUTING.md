# Contributing to Bridge Core Engine

Thanks for your interest in Bridge. This document covers how to set up a development environment, what we expect from contributions, and how to get your changes merged.

---

## Development Setup

### Requirements

- **Python** 3.11 – 3.13
- **Redis** 7+ (local loopback for development)
- **Git**

### First-time setup

```bash
git clone <repo-url>
cd Bridge-core

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp core.env.template core.env
# Edit core.env: set at least one LLM provider (e.g. FIREWORKS_API_KEY + FIREWORKS_MODEL,
# or OLLAMA_MODEL for a local Ollama instance)
```

### Running the server locally

```bash
.venv/bin/python bridge_core.py
# http://127.0.0.1:8766 — loopback only in development
```

### Running tests

```bash
.venv/bin/python -m compileall .
.venv/bin/python -m unittest discover -s tests -v
```

Tests use an in-memory Redis substitute and mocked LLM transports. **No live services or network calls are required.** If your change breaks a test, fix the test or explain why the test itself is wrong.

---

## Project Conventions

### Before you write code

Bridge has two authoritative documents that govern scope and architecture:

- **`BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md`** — Locked scope and architectural decisions. If your contribution changes or contradicts something here, open a discussion first.
- **`BRIDGE_CORE_ENGINE_SPEC.md`** — The living implementation contract. This is the ground truth for how things should work.

Read the relevant sections before proposing changes. We reject drive-by refactors that violate documented intent.

### Code style

- **Python**: PEP 8, 100-character soft line limit.
- **Type hints**: required on all public functions and methods. Use `from __future__ import annotations` where it helps readability.
- **Docstrings**: Google style for modules, classes, and public APIs.
- **No print debugging**: use the structured logging already wired in `core/app.py`.

### Module boundaries

Keep the repository layout clean:

- `core/` — runtime logic only. No I/O that bypasses the app lifespan.
- `identity/` — owner-authored templates. The engine never writes here.
- `tests/` — unit tests, no live services. Mock at the transport layer.

### Configuration discipline

- All new tunables belong in `core.env.template` with safe, behavior-inert defaults.
- Secrets must never be exposed via `/status`, logs, or error messages.
- Boolean and numeric env vars must fail startup with a clear message if malformed.

---

## How to Contribute

### Reporting bugs

Open an issue with:

1. **Bridge version** (`/health` output).
2. **Environment**: OS, Python version, Redis version.
3. **Steps to reproduce** — minimal, self-contained.
4. **Expected vs. actual behavior**.
5. **Logs** — redact any API keys before posting.

### Proposing features

Bridge is intentionally scoped by milestone. Before opening a feature request:

- Check `BRIDGE_CORE_ENGINE_IMPLEMENTATION_PLAN.md`. If the feature is already listed for a later milestone, it may be intentionally deferred.
- If it is not listed, open a **discussion** (not a PR) explaining:
  - What problem it solves.
  - Why it belongs in Bridge rather than in a client or wrapper.
  - Whether it fits behind a flag that defaults OFF (our standard for post-0.1.0 capabilities).

### Security issues

**Do not open public issues for security vulnerabilities.**

Bridge is designed to run inside a private Tailscale tailnet with no public exposure. If you discover a way to bypass this boundary or leak secrets, email the maintainer directly or use GitHub private vulnerability reporting.

---

## Pull Request Guidelines

### Branch naming

- `fix/<short-description>` — bug fixes
- `feat/<short-description>` — new capabilities
- `docs/<short-description>` — documentation-only changes

### PR checklist

- [ ] `python -m compileall .` passes cleanly.
- [ ] `python -m unittest discover -s tests -v` passes.
- [ ] New code has type hints and docstrings.
- [ ] New configuration variables are documented in `core.env.template`.
- [ ] No secrets, API keys, or tailnet addresses are hardcoded.
- [ ] Commit messages explain *why*, not just *what*.

### Review process

1. **Scope check** — does the PR align with `IMPLEMENTATION_PLAN.md` and `SPEC.md`?
2. **Test check** — do tests pass? Are new features covered?
3. **Security check** — no new public exposure, no secret leakage.
4. **Merge** — squash-merge with a descriptive commit message.

---

## Architecture Notes for Contributors

### The Tailscale-only boundary

Bridge is **never** meant to be exposed to the public internet. Every contribution must respect this:

- Do not add public auth layers (OAuth, bearer tokens, API keys) as a substitute for tailnet isolation.
- Do not suggest reverse proxies, port forwarding, or Tailscale Funnel in documentation or examples.
- If you add a new network-facing endpoint, it must follow the existing bind-validation logic in `core/tailscale.py`.

### Milestone flags

Features arriving after 0.1.0 (speech, needs, schedule, work mode, memory, initiative) are expected to land **behind configuration flags that default to OFF**. This keeps the core stable while the surface area grows. Follow this pattern unless you have explicit maintainer approval to do otherwise.

### LLM provider chain

The router in `core/llm.py` handles failover across multiple backends. If you add a new provider:

- Implement the same retry/failover contract as existing providers.
- Add a test in `tests/` that mocks the transport layer.
- Document the required env vars in `core.env.template`.

---

## Community

- **Questions?** Open a discussion rather than an issue.
- **Want to see what Bridge becomes with a full world attached?** Check out [Akane's Project Instagram](https://instagram.com/satomi.kazoku), the companion project that inspired this engine.

Thanks for helping make Bridge better.
