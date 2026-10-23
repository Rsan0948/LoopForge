"""Isolated real-harness/n8n transport qualification with SCRIPTED reports.

Default mode makes no Ollama calls or product edits. Fourteen scripted units
force a bounded parent continuation. --real-repair runs an isolated frozen Brook
regression through actual model coding, fixed checks and fresh review.
Workflows are retained inactive for inspection. This proves
transport/reducer mechanics, not model review quality.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

import loopforge.entrypoints.campaign as campaign_module
import loopforge.entrypoints.campaign_review as review_module
from loopforge.adapters.scripted import ScriptedModel
from loopforge.domain.actions import ActionProposal
from loopforge.domain.types import ActionId, UsageDelta
from loopforge.entrypoints.campaign_coding import local_git, local_git_bytes
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingTaskConfig
from loopforge.ports.reporting import ReportTurn
from scripts.qualify_brook_repair import SOURCE, check_fixture


def main():  # noqa: PLR0912, PLR0915 - isolated serial transport lifecycle
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--real-repair", action="store_true")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path.home() / "ai-platform/infra"))
    import n8n_dashboard as dashboard  # noqa: PLC0415 - local operator integration
    from automation_hub.service import HubService  # noqa: PLC0415

    hub = HubService(dashboard.BASE, dashboard.n8n, dashboard)
    real_build = campaign_module.build_review

    def scripted_build(*pos, **kw):
        kw["model"] = ScriptedModel(
            [ActionProposal(ActionId("scripted-finalize"), "finalize_report", {})]
        )
        runtime, tools, live = real_build(*pos, **kw)
        config = pos[0] if pos else kw["config"]
        runtime.model = review_module.ReportingReviewModel(kw["model"], tools, config, kw["store"])
        return runtime, tools, live

    class ScriptedReporter:
        def __init__(self, *pos, **kw):
            pass

        def generate(self, *_pos, **_kw):
            return ReportTurn(
                json.dumps(
                    {
                        "summary": "SCRIPTED transport only; no model quality judgment.",
                        "findings": [],
                        "unresolved": [],
                    }
                ),
                UsageDelta(),
            )

        def close(self):
            pass

    if not args.real_repair:
        campaign_module.build_review = scripted_build
        review_module.OllamaReporting = ScriptedReporter
    server = uvicorn.Server(
        uvicorn.Config(
            campaign_module.create_app(args.output / "harness"),
            host="0.0.0.0",
            port=8125,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    installed = []
    with httpx.Client(timeout=30) as client:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        if not server.started:
            message = "Qualification service failed to start"
            raise RuntimeError(message)
        live = client.get("http://localhost:8124/campaigns").raise_for_status().json()["campaigns"]
        if any(c["state"]["status"] != "parked" or c["worker_alive"] or c["slot"] for c in live):
            message = "Live campaigns must stay parked"
            raise RuntimeError(message)
        source = Path(".loopforge/campaign/coding/coding_02f99bb76e82").resolve()
        config = CampaignConfig(
            repository=str(source),
            project="brook",
            revision="9eba77fa4680348b4d767da73ddee1c82bba0cfc",
            scope=[
                "apps/dashboard/lib/workspace/export.ts",
                "apps/dashboard/lib/workspace/export.test.ts",
            ],
            rounds=1,
            max_iterations=32,
            max_tokens=100000,
            max_seconds=120,
        )
        original = None
        if args.real_repair:
            source = Path(
                ".loopforge/campaign/monitor/qualification/brook-repair-envelope-diagnostic/seed"
            ).resolve()
            config = config.model_copy(
                update={
                    "repository": str(source),
                    "revision": local_git(source, "rev-parse", "HEAD"),
                    "max_seconds": 180,
                }
            )
            with sqlite3.connect(".loopforge/campaign/campaigns.sqlite") as db:
                raw = db.execute(
                    "SELECT config FROM coding_tasks WHERE id=?", ("coding_02f99bb76e82",)
                ).fetchone()[0]
            original = CodingTaskConfig.model_validate_json(raw)
            seeded = check_fixture(source, original)
            if seeded.succeeded or seeded.exit_code != 1:
                message = "Frozen seeded regression must fail an actual assertion before model work"
                raise RuntimeError(message)
            (args.output / "seed-check.txt").write_text(seeded.stdout + seeded.stderr)
        campaign = (
            client.post("http://localhost:8125/campaigns", json=config.model_dump())
            .raise_for_status()
            .json()["id"]
        )
        task_id = None
        if args.real_repair:
            task = original.model_copy(
                update={
                    "objective": (
                        "Correct calendar folding UTF8 round-trip corruption for mixed Unicode "
                        "input. Use review_feedback for the immutable check, inspect the relevant "
                        "source, make the smallest source-only correction. Existing tests "
                        "remain immutable."
                    ),
                    "branch": "qualification/n8n-real-repair",
                    "allowed_paths": [SOURCE],
                    "max_attempts": 1,
                    "max_review_attempts": 1,
                    "evidence": (
                        "Isolated frozen seed; actual immutable assertion "
                        "reproduced before inference."
                    ),
                }
            )
            task_id = (
                client.post(
                    "http://localhost:8125/campaigns/" + campaign + "/coding",
                    json=task.model_dump(),
                )
                .raise_for_status()
                .json()["id"]
            )
        suffix = uuid4().hex
        parent_live = hub.checked(hub.n8n("GET", "/api/v1/workflows/" + hub.setting("driver_id")))
        credential = next(n for n in parent_live["nodes"] if n["type"] == "n8n-nodes-base.webhook")[
            "credentials"
        ]["httpHeaderAuth"]["id"]
        child = json.loads((hub.template.parent / "bounded-unit.json").read_text())
        for node in child["nodes"]:
            if node["name"] == "Unit Identity":
                node["parameters"]["jsCode"] = node["parameters"]["jsCode"].replace(
                    ":8124", ":8125"
                )
        assert child["settings"]["saveDataSuccessExecution"] == "all"
        child["name"] = "QUALIFICATION scripted bounded unit " + suffix
        child_payload = {k: child[k] for k in ("name", "nodes", "connections", "settings")}
        hook_path = "qualification-" + suffix
        parent = hub.driver_template(credential, hook_path)
        assert parent["settings"]["saveDataSuccessExecution"] == "all"
        parent["name"] = "QUALIFICATION scripted campaign " + suffix
        for node in parent["nodes"]:
            if node["name"] == "Campaign Config":
                node["parameters"]["jsCode"] = node["parameters"]["jsCode"].replace(
                    ":8124", ":8125"
                )
        try:
            created = hub.checked(hub.n8n("POST", "/api/v1/workflows", child_payload))
            child_id = created["id"]
            installed.append(child_id)
            hub.checked(hub.n8n("POST", "/api/v1/workflows/" + child_id + "/activate"))
            next(n for n in parent["nodes"] if n["name"] == "Run Bounded Unit")["parameters"][
                "workflowId"
            ]["value"] = child_id
            parent_payload = {k: parent[k] for k in ("name", "nodes", "connections", "settings")}
            created = hub.checked(hub.n8n("POST", "/api/v1/workflows", parent_payload))
            parent_id = created["id"]
            installed.append(parent_id)
            (args.output / "workflows.json").write_text(
                json.dumps(
                    {
                        "parent": parent_id,
                        "child": child_id,
                        "template_sha256": hashlib.sha256(hub.template.read_bytes()).hexdigest(),
                        "limitation": ("Clone differs in port, child ID, names and hook identity."),
                    },
                    indent=2,
                )
            )
            hub.checked(hub.n8n("POST", "/api/v1/workflows/" + parent_id + "/activate"))
            url = "http://localhost:5678/webhook/" + hook_path
            payload = {"campaignId": campaign, "launchId": uuid4().hex}
            for _ in range(20):
                denied = client.post(url, json=payload)
                if denied.status_code != 404:
                    break
                time.sleep(0.25)
            if denied.status_code not in (401, 403):
                message = "Webhook did not deny missing authentication"
                raise RuntimeError(message)
            headers = {"X-HomeOS-Launch": hub.setting("driver_secret")}
            client.post(url, headers=headers, json=payload).raise_for_status()
            latest = None
            for _ in range(240):
                checkpoint = (
                    client.get("http://localhost:8125/campaigns/" + campaign + "/checkpoint")
                    .raise_for_status()
                    .json()
                )
                current = (checkpoint["state"]["cursor"], checkpoint["state"]["status"])
                if current != latest:
                    print(json.dumps({"cursor": current[0], "status": current[1]}), flush=True)
                    latest = current
                if (
                    current[0] >= 14
                    or current[1] == "attention"
                    or (
                        args.real_repair
                        and not checkpoint["runnable"]
                        and not checkpoint["worker_alive"]
                        and checkpoint["slot"] is None
                    )
                ):
                    break
                time.sleep(1)
            # The harness can finish before the waiting child polls once more.
            # Keep the service alive until both n8n parents and all children finish.
            for _ in range(60):
                active = []
                for status in ("running", "waiting"):
                    active.extend(
                        hub.checked(
                            hub.n8n("GET", "/api/v1/executions?status=" + status + "&limit=100")
                        ).get("data", [])
                    )
                if not any(e.get("workflowId") in installed for e in active):
                    break
                time.sleep(1)
            executions = []
            for workflow in installed:
                executions.extend(
                    hub.checked(
                        hub.n8n("GET", "/api/v1/executions?workflowId=" + workflow + "&limit=100")
                    ).get("data", [])
                )
            transport_passed = len(executions) >= (3 if args.real_repair else 16) and all(
                e.get("status") == "success" for e in executions
            )
            (args.output / "n8n-executions.json").write_text(json.dumps(executions, indent=2))
            ledger = (
                client.get("http://localhost:8125/campaigns/" + campaign + "/ledger")
                .raise_for_status()
                .json()
            )
            dispatches = (
                client.get(
                    "http://localhost:8125/campaigns/" + campaign + "/dispatches",
                    params={"launch_id": payload["launchId"]},
                )
                .raise_for_status()
                .json()
            )
            (args.output / "ledger.json").write_text(json.dumps(ledger, indent=2))
            repair_passed = False
            if args.real_repair:
                detail = (
                    client.get("http://localhost:8125/campaigns/" + campaign + "/coding/" + task_id)
                    .raise_for_status()
                    .json()
                )
                (args.output / "coding-task.json").write_text(json.dumps(detail, indent=2))
                candidate = args.output / "harness/coding" / task_id
                fresh = check_fixture(candidate, original)
                diff = local_git_bytes(candidate, "diff", "--binary", "HEAD", "--")
                (args.output / "candidate.diff").write_bytes(diff)
                (args.output / "fresh-check.txt").write_text(fresh.stdout + fresh.stderr)
                repair_passed = (
                    detail["state"]["status"] == "patch_ready"
                    and fresh.succeeded
                    and local_git(candidate, "diff", "--name-only", "HEAD", "--").splitlines()
                    == [SOURCE]
                )
            receipt = {
                "passed": transport_passed
                and (repair_passed if args.real_repair else checkpoint["state"]["cursor"] == 14)
                and not checkpoint["worker_alive"]
                and checkpoint["slot"] is None,
                "campaign": campaign,
                "workflows": installed,
                "dispatches": dispatches,
                "scripted": not args.real_repair,
                "real_repair_passed": repair_passed,
                "n8n_terminal_success": transport_passed,
                "retention_override": "None; canary clones inherit production template retention",
                "model_quality_proven": False,
                "live_campaigns_unchanged": True,
            }
            (args.output / "result.json").write_text(json.dumps(receipt, indent=2))
            artifact = hub.save_artifact(
                (
                    "Real repair qualification "
                    if args.real_repair
                    else "Scripted n8n transport qualification "
                )
                + suffix,
                "json",
                json.dumps(receipt, indent=2),
                "qualification",
                "Scripted transport evidence; no model quality judgment",
                repository="brook",
                source_key="qualification:" + suffix,
            )
            receipt["hub_artifact_id"] = artifact["id"]
            (args.output / "result.json").write_text(json.dumps(receipt, indent=2))
            print(json.dumps(receipt), flush=True)
        finally:
            for identity in reversed(installed):
                hub.checked(hub.n8n("POST", "/api/v1/workflows/" + identity + "/deactivate"))
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
