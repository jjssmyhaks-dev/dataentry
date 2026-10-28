#!/usr/bin/env python
"""End-to-end smoke run against a live engine.

Ingests the manufacturing pack's evaluation set through the HTTP API exactly as the
web app would, then walks the review queue: list, inspect, approve, check the
voucher and the agent command that came out of it.

    python scripts/smoke_api.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.settings import ROOT, get_settings  # noqa: E402

DOCS = ROOT / "packages" / "packs" / "manufacturing" / "eval" / "documents"


def headers(tenant_slug: str) -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.engine_service_token}",
        "X-Dev-Tenant": tenant_slug,
        "X-Actor-Id": "dev-reviewer",
        "X-Actor-Role": "owner",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default="precision-pipes")
    parser.add_argument("--api", default="http://127.0.0.1:8088")
    parser.add_argument("--approve", default=None, help="approve the first pending item in this doc type")
    args = parser.parse_args()

    h = headers(args.tenant)
    client = httpx.Client(base_url=args.api, headers=h, timeout=60)

    health = client.get("/health").json()
    print(f"engine {health['version']} | database {health['database']} | llm {health['llm_enabled']}")
    print()

    print("-- intake -----------------------------------------------")
    ingest_results = []
    for path in sorted(DOCS.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        response = client.post(
            "/v1/documents",
            json={
                "text": text,
                "file_name": f"{path.stem}.pdf",
                "channel": "email",
                "sender": "accounts@supplier.example",
                "mime_type": "application/pdf",
            },
        )
        response.raise_for_status()
        payload = response.json()
        ingest_results.append((path.stem, payload))
        decisions = [f"{r['decision']}/{r.get('status', '-')}" for r in payload["records"]]
        print(
            f"{path.stem:<30} {payload['doc_type'] or '-':<17} "
            f"{payload['status']:<12} records={len(payload['records'])} {decisions}"
        )
        for warning in payload["warnings"][:2]:
            print(f"    ! {warning[:120]}")

    print()
    print("-- review queue -----------------------------------------")
    queue = client.get("/v1/review/queue").json()
    for status, data in sorted(queue["counts"].items()):
        print(f"  {status:<26} {data['count']:>3}  (rule failures: {data['rule_failures']})")
    print(f"  pending items shown: {len(queue['items'])}")
    for item in queue["items"][:6]:
        reasons = item.get("reasons") or []
        first = reasons[0]["message"][:80] if reasons else ""
        print(
            f"    p{item['priority']:<4} {item['doc_type']:<17} "
            f"{str(item['amount_total'] or ''):>12} {item['priority_band']:<15} {first}"
        )

    if not queue["items"]:
        print("nothing queued; nothing to approve")
        return 0

    target = next(
        (
            i
            for i in queue["items"]
            if i["priority_band"] == "low_confidence"
            and (args.approve is None or i["doc_type"] == args.approve)
        ),
        None,
    )
    if target is None:
        target = next(
            (i for i in queue["items"] if args.approve is None or i["doc_type"] == args.approve),
            None,
        )
    if target is None:
        print(f"no pending item matches {args.approve!r}")
        return 1

    print()
    print(f"-- reviewing {target['id']} ------------------------------")
    detail = client.get(f"/v1/review/items/{target['id']}").json()
    print(f"  document: {detail['document']['file_name']} ({detail['document']['intake_channel']})")
    for rule in detail["rules"]:
        if rule["outcome"] in {"fail", "warn"}:
            print(f"  {rule['outcome']:<5} {rule['rule_key']:<28} {(rule['message'] or '')[:90]}")
    preview = detail.get("voucher_preview") or {}
    voucher = preview.get("voucher") or {}
    print(f"  voucher preview: {voucher.get('voucher_type')} | balanced={preview.get('balanced')}")
    for line in voucher.get("lines") or []:
        side = "Dr" if line["is_debit"] else "Cr"
        print(f"    {side} {line.get('ledger') or line.get('stock_item') or '?':<34} {line['amount']}")
    if preview.get("warnings"):
        for warning in preview["warnings"]:
            print(f"    ! {warning}")

    print()
    print("-- approve ---------------------------------------------")
    approval = client.post(
        f"/v1/review/items/{target['id']}/approve", json={"note": "smoke run"}
    )
    if approval.status_code >= 400:
        print(f"  approve failed: {approval.status_code} {approval.text[:300]}")
        return 1
    result = approval.json()
    print(f"  item: {result['item_status']} | voucher: {result['voucher_status']}")
    print(f"  rules now: {json.dumps(result['rule_summary'])}")

    print()
    print("-- bridge agent queue -----------------------------------")
    if result.get("voucher_id"):
        voucher = client.get(f"/v1/vouchers/{result['voucher_id']}").json()["voucher"]
        print(f"  voucher {voucher['id']} status={voucher['status']}")
        print(f"  narration: {voucher['narration']}")
        print(f"  idempotency key: {voucher['idempotency_key']}")
    agent_headers = {
        "X-Agent-Credential-Id": f"agent-{args.tenant}-pc1",
    }
    commands = httpx.get(f"{args.api}/v1/agent/commands", headers=agent_headers, timeout=30).json()
    for command in commands.get("commands", []):
        payload = command["payload"]
        signed = bool(payload.get("signature"))
        print(
            f"  command {command['id']} {command['kind']} signed={signed} "
            f"expires={command['expires_at']}"
        )
        print(f"    xml bytes: {len(payload.get('xml') or '')}")
    if not commands.get("commands"):
        print("  no commands queued")

    print()
    print("-- autonomy ---------------------------------------------")
    autonomy = client.get("/v1/autonomy").json()
    print(f"  stage: {autonomy['stage']['name']} | mode: {autonomy['posting_mode']}")
    print(f"  error rate: {autonomy['error_rate']}")
    for check in autonomy["gate"]["checks"]:
        state = "pass" if check["passed"] else "needs work"
        print(f"    {check['label']:<48} {str(check['actual'])[:10]:>10} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
