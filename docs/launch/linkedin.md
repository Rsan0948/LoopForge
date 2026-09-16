# LinkedIn launch copy

## Positioning

LoopForge is primarily a **usable reference architecture** for deterministic,
transparent, and governable agentic systems. The working software-engineering
runtime and console are important because they make the architecture executable
and falsifiable; they are evidence for the thesis, not the whole thesis.

Preferred one-line description:

> A usable reference architecture for deterministic, transparent, and
> governable agentic systems.

Preferred hook:

> Models propose. The runtime decides.

## Launch post

I've open-sourced LoopForge: a usable reference architecture for building
deterministic, transparent, and governable AI agentic systems.

Its core idea is simple:

**Models propose. The runtime decides.**

The software-engineering agent and operator console are real and usable, but
they are the reference implementation—not the entire thesis. The larger goal
is to demonstrate a control-plane architecture in which probabilistic models
operate inside deterministic boundaries:

- authority and tool risk are owned by code, not model output
- runs are event-sourced, replayable, resumable, and explainable
- verification is independent of generation
- budgets, retries, idempotency, circuit breakers, and termination are explicit
- untrusted repositories execute through a capability-negotiated container sandbox
- runtime policies are evaluated on a locked benchmark with deterministic graders,
  trajectory metrics, and Pareto comparisons—not selected from one good demo

LoopForge includes a local operator console for starting, steering, pausing,
approving, replaying, and inspecting runs. That usability matters: a reference
architecture should be possible to run, challenge, measure, and adapt—not only
read about in a diagram.

The project is public beta software and intentionally documents its limits. I
would especially value feedback from people working on agent reliability,
evaluation, sandboxing, observability, and human-in-the-loop systems.

Install:

```text
pipx install loopforge-console
loopforge console
```

GitHub: https://github.com/Rsan0948/LoopForge

#OpenSource #AIAgents #AgenticAI #SoftwareArchitecture #AIGovernance #LLM #Python

## Carousel order

1. `hero-linkedin-reference-architecture.png` — thesis and identity
2. `flow-how-it-works.png` — control-plane architecture
3. `live-run.png` — usable reference implementation
4. `evals-benchmark.png` — reproducible evaluation evidence
5. `policy-registry.png` — governed adaptation within fixed authority

Always write “LoopForge by Ruben Sanchez” or include the exact repository URL;
several unrelated projects use the same name, and the bare `loopforge` package
on PyPI is not this project.
