# MLX comparison lab setup

The local inference environment is installed and verified. No paired performance
or coding-quality trials have run. No paid provider calls were made.

## Installed and pinned

- Apple Silicon, 64 GiB unified memory; Metal availability checked.
- Separate Python 3.12 environment: `.loopforge/mlx-lab/.venv`.
- MLX 0.32.4, MLX Metal 0.32.4, MLX-LM 0.32.2; inference dependencies pinned in
  `scripts/mlx-lab-requirements.txt`.
- `mlx-community/Qwen3.6-35B-A3B-4bit`, Hugging Face revision
  `38740b847e4cb78f352aba30aa41c76e08e6eb46`, downloaded using the official
  Hugging Face snapshot downloader into its standard shared cache. Roughly
  20.4 GB of model files; rerunning setup reuses that cache.
- Model architecture is supported by the installed upstream loader; no remote
  Python model code is enabled and no vendor package was patched.

The existing LoopForge environment, runtime policies, application candidate and
Ollama model files were not changed. Ollama's idle loaded model was unloaded for
exclusive smoke measurements; its downloaded weights remain available.

## Reused inputs and controls

`comparison-inputs.json` records three preserved reconciliation task states,
source revisions and file hashes, frozen intake hashes, the existing pinned
container image, fixed checks, independent acceptance probes, and six exact
native Ollama request captures. It references historical journals rather than
reconstructing conversations from summaries. The existing Docker/image
availability preflight passed for all three task configurations.

The previous live checks are historical verification evidence; this setup did
not rerun the coding-task checks or execute repository code. The paired runner
must revalidate hashes and use disposable workspaces before starting any trial.
Suggested bounds in the manifest are proposals, not a launched experiment.

`ModelLease` coordinates managed local inference. The launcher refuses to compete
with loaded Ollama models unless `--unload-ollama` is explicitly supplied. Upstream
`mlx_lm.server` runs on loopback port 18181, offline from Hugging Face after setup,
with one prompt/decode at a time, two cache entries and a 4 GiB cache byte bound.
This is an inference service, not a code-execution sandbox. The validated container
capability contracts remain the required execution boundary for future coding trials.

## Verification performed

Three bounded exchanges passed using the downloaded model and upstream server:

1. Thinking disabled: exactly one structured `read_file` call with the expected
   argument, without executing that tool.
2. Native conversation continuation: consumed a simulated tool result and returned
   `ready`. The tool list remained unchanged; server usage recorded 292 cached
   prompt tokens out of 360 on the follow-up.
3. Thinking enabled with temperature/top-p/presence settings reused from the
   existing Qwen native profile: exactly one correctly structured tool call,
   completing within a 512-token allowance.

Raw requests/responses and usage are in `artifacts/smoke.json`. This is protocol
compatibility and cache-reuse evidence, not proof of equal coding quality or an
MLX-versus-Ollama speed improvement. Penalty history scopes and prompt rendering
still need explicit matching in a paired comparison; matching scalar sampling
settings alone does not establish identical inference semantics.

The server was stopped and the model lease released after the smoke checks.
Setup was rerun successfully against the downloaded cache. The launcher also
handles immediate restarts after a closed socket enters TIME_WAIT and cleans
up its own server process on Ctrl+C or SIGTERM. Ruff checks and formatting pass
for both new scripts; Python compilation and CLI help/status were checked.

## Commands

Run from the LoopForge repository root:

```bash
# Inspect installation and saved smoke evidence; no inference.
.venv/bin/python scripts/mlx_lab.py status

# Reproduce installation/download using pinned versions and revision.
.venv/bin/python scripts/mlx_lab.py setup

# Repeat the bounded protocol smoke test; stops its server afterward.
.venv/bin/python scripts/mlx_lab.py smoke --unload-ollama

# Run the upstream local server until Ctrl+C.
.venv/bin/python scripts/mlx_lab.py serve --unload-ollama

# Rebuild task/request inventory without inference or candidate execution.
.venv/bin/python scripts/prepare_mlx_comparison.py
```

Local artifacts under `.loopforge/mlx-lab/` include `model.json`,
`comparison-inputs.json`, `artifacts/environment-freeze.txt`, `artifacts/smoke.json`,
and installation/server logs. These paths are intentionally gitignored.

## Comparison limits

The baseline Ollama model is `qwen3.6:35b-a3b` in GGUF Q4_K_M; this MLX artifact
uses affine 4-bit quantization. Both identify the same base model, but the weight
representations are different. A result compares these deployments, not a pure
engine-only effect. Record both model digests, tokenizer/template behavior,
reasoning/output limits, sampling and cache conditions for every trial.

Next work is a bounded standalone replay/coding runner using these assets and
existing confined tools and verifiers. The production supervisor has not been
migrated to MLX. Fresh matched Ollama controls and repeated outcomes are still
needed before choosing an engine.

References: [upstream MLX-LM](https://github.com/ml-explore/mlx-lm),
[server documentation](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md),
[pinned model](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-4bit/tree/38740b847e4cb78f352aba30aa41c76e08e6eb46).
