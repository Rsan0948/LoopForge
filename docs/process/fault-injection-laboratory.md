# Fault-injection laboratory

PACS-004 turns LoopForge reliability behavior into an explicit adversarial test surface. The
laboratory is deterministic and uses no live model/provider.

## Fault matrix

| Fault | Injection point | Expected runtime behavior | Safety property |
|---|---|---|---|
| transient timeout | before delegated tool execution | bounded retry/backoff | retry policy remains runtime-owned |
| ambiguous remote success | after real side effect, before observed success | reuse same keyed action identity | exactly one logical side effect |
| process interruption | after side effect while action remains in-flight | durable resume if replay-safe | no blind replay of unsafe writes |
| duplicate event delivery | event-store append | reject duplicate global event ID | authoritative history is append-only/unique |
| malformed model response | model port boundary | explicit `ModelContractError` | corrupt provider payload does not mutate action state |
| malformed tool response | tool port boundary | explicit `ToolContractError` | ambiguous in-flight state remains visible/recoverable |
| budget exhaustion | after model usage debit, before tool authorization | terminal budget stop | hard budget wins before side effect |

## Design rule

Fault adapters report observations; they do not declare policy. A timeout or lost response is an
operational fact. Whether LoopForge retries it is derived independently from the registered tool
contract, action journal, remaining budget, and reliability policy.

## Scope boundary

This lab simulates timeout/process interruption at the adapter boundary. It does not claim generic
OS-level hard cancellation of arbitrary child code. Process/resource isolation is the PACS-005
sandbox contract and later hardened sandbox adapters.
