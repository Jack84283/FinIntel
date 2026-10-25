"""Review an existing FinIntel acceptance report without rerunning market-data scans.

Exit codes: 0 = no blockers and no review items; 2 = manual review required;
1 = definite failure or invalid report. A zero exit code is not a certification
that all market data is accurate.
"""

import argparse
import json
from pathlib import Path


def assess(report: dict) -> dict:
    required = ("integrity", "coverage", "corporate", "provider", "performance")
    missing = [section for section in required if not isinstance(report.get(section), dict)]
    blockers = []
    review = []
    if missing:
        blockers.append("Missing report sections: " + ", ".join(missing))
        return {"verdict": "fail", "blockers": blockers, "review": review}

    integrity = report["integrity"]
    issues = integrity.get("issues")
    if not isinstance(issues, dict):
        blockers.append("Missing OHLCV issue counts")
    elif any(not isinstance(value, int) or value < 0 for value in issues.values()):
        blockers.append("Invalid OHLCV issue counts")
    elif sum(issues.values()) > 0:
        blockers.append(f"OHLCV integrity issues: {sum(issues.values()):,}")

    performance = report["performance"]
    accepted = performance.get("all_acceptance_targets_met")
    if accepted is False:
        blockers.append("Performance acceptance targets failed")
    elif accepted is not True:
        blockers.append("Performance acceptance results unavailable")

    coverage = report["coverage"]
    count = coverage.get("xnys_internal_gap_candidates")
    if not isinstance(count, int) or count < 0:
        blockers.append("XNYS gap candidate count unavailable")
    elif count:
        review.append(
            f"{count:,} XNYS internal gap candidates require listing/venue/halt review; "
            "do not interpret as confirmed missing rows"
        )

    corporate = report["corporate"]
    jumps = corporate.get("corporate_action_jump_candidate_count")
    if jumps is None:
        blockers.append("Corporate action screening count unavailable")
    elif isinstance(jumps, int) and jumps > 0:
        review.append(f"{jumps:,} large price jump candidates require review")

    provider = report["provider"]
    pairs = provider.get("cross_provider_pairs_compared")
    divergence = provider.get("cross_provider_close_divergences_over_1pct")
    if pairs is None or divergence is None:
        blockers.append("Provider comparison counts unavailable")
    else:
        if pairs == 0:
            review.append("No overlapping provider pairs were compared")
        if isinstance(divergence, int) and divergence > 0:
            review.append(f"{divergence:,} provider price differences over 1%")

    reference_error = report.get("corporate_reference_error")
    if reference_error:
        review.append("Optional corporate-reference lookup failed: " + str(reference_error))
    reference = report.get("corporate_reference")
    if isinstance(reference, dict):
        events = reference.get("events", [])
        if not isinstance(events, list):
            blockers.append("Corporate reference events malformed")
        else:
            needs_review = sum(
                isinstance(event, dict) and event.get("check") == "manual_review"
                for event in events
            )
            if needs_review:
                review.append(f"{needs_review:,} split-reference events require manual review")
    elif not reference_error:
        review.append("Optional live corporate-reference lookup not present in report")

    return {
        "verdict": "fail" if blockers else "review" if review else "pass",
        "blockers": blockers,
        "review": review,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Existing local acceptance-report.json")
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("Expected JSON object")
        result = assess(report)
    except (OSError, ValueError) as exc:
        print(f"Report cannot be reviewed: {exc}")
        return 1
    print("Verdict:", result["verdict"].upper())
    for message in result["blockers"]:
        print("BLOCKER:", message)
    for message in result["review"]:
        print("REVIEW:", message)
    return {"pass": 0, "review": 2, "fail": 1}[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
