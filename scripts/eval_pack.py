#!/usr/bin/env python
"""Run a pack's evaluation set and report against the release gate.

    python scripts/eval_pack.py                 # the whole set
    python scripts/eval_pack.py --json out.json # machine-readable report
    python scripts/eval_pack.py --fail-under 0.97
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.evaluation import run_evaluation  # noqa: E402
from engine.packs.loader import load_pack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a pack")
    parser.add_argument("--pack", default="manufacturing")
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--json", default=None, help="write the full report here")
    parser.add_argument("--fail-under", type=float, default=None)
    args = parser.parse_args()

    pack = load_pack(args.pack)
    report = run_evaluation(pack, cases=args.case)
    summary = report.summary()

    print(f"pack {summary['pack']}   source: {pack.evaluation.source or 'unknown'}")
    if pack.evaluation.source_caveat:
        print(f"! {pack.evaluation.source_caveat[:150]}")
    print()
    print(f"{'case':<32}{'type':<17}{'fields':>10}{'acc':>8}{'rules':>8}  doc")
    print("-" * 90)
    for case in report.cases:
        accuracy = f"{case.field_accuracy:.2f}" if case.field_accuracy is not None else "n/a"
        rules = "ok" if case.rules_agree else "DIFF"
        print(
            f"{case.case_id:<32}{case.doc_type:<17}"
            f"{case.field_correct:>4}/{case.field_total:<5}{accuracy:>8}{rules:>8}  "
            f"{'ok' if case.document_correct else 'partial'}"
        )
    print("-" * 90)
    print(
        f"field accuracy {summary['field_accuracy']:.4f} "
        f"({summary['fields']['correct']}/{summary['fields']['total']}) | "
        f"document accuracy {summary['document_accuracy']:.4f} | "
        f"classification {summary['classification_accuracy']:.4f} | "
        f"rule agreement {summary['rule_agreement']:.4f}"
    )
    gate = summary["gate"]
    print(f"stage 1 gate (field accuracy >= {gate['target']}): {'PASS' if gate['passed'] else 'NOT YET'}")

    mismatches = [m for case in report.cases for m in case.mismatches]
    if mismatches:
        print()
        print("mismatches:")
        for mismatch in mismatches[:20]:
            print(f"  {mismatch['field']}: expected {mismatch['expected']!r} got {mismatch['actual']!r}")

    rule_diffs = [e for case in report.cases for e in case.rule_expectations if not e["agrees"]]
    if rule_diffs:
        print()
        print("rule expectations not met:")
        for diff in rule_diffs:
            print(f"  {diff['rule']}: expected {diff['expected']}, got {diff['actual']} - {diff['message']}")

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(
                {
                    "summary": summary,
                    "cases": [
                        {
                            "id": c.case_id,
                            "doc_type": c.doc_type,
                            "field_accuracy": c.field_accuracy,
                            "document_correct": c.document_correct,
                            "rules_agree": c.rules_agree,
                            "mismatches": c.mismatches,
                            "rule_expectations": c.rule_expectations,
                            "notes": c.notes,
                        }
                        for c in report.cases
                    ],
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nfull report written to {args.json}")

    if args.fail_under is not None and summary["field_accuracy"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
