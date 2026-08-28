"""Workload definitions that bind through LoopForge ports.

The runtime core stays workload-agnostic. This package contains the
software-repair reference workload: code-owned task definitions, fixture
repositories, the deterministic verifier stack, evidence artifact collectors,
and the context-trust adapter for untrusted repository content. Nothing here
is imported by ``loopforge.domain``, ``loopforge.ports``, or
``loopforge.application``; entrypoints wire workloads into the runtime.
"""
