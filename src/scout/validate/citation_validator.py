"""Mechanical Citation Resolution Validator (PRD FR-5.4).

Verifies that:
1. Every claim in the report cites at least one finding_id.
2. Every cited finding_id resolves to an accepted, verified finding in the run's registry.
3. No hallucinated or unresolved citation IDs exist in the report.
"""

from __future__ import annotations

from typing import Dict, List

from scout.schemas import Claim, Finding, Report, ValidationResult


def validate_report_citations(
    report: Report,
    findings_registry: Dict[str, Finding],
) -> ValidationResult:
    """Mechanically checks that 100% of citations in the report resolve to verified findings."""
    unresolved: List[str] = []
    missing_sources: List[str] = []
    dropped_claims: List[Claim] = []

    for section in report.sections:
        for claim in section.claims:
            claim_has_unresolved = False
            if not claim.finding_ids:
                claim_has_unresolved = True
                unresolved.append(f"Claim '{claim.text[:40]}...' has empty finding_ids")

            for fid in claim.finding_ids:
                if fid not in findings_registry:
                    claim_has_unresolved = True
                    unresolved.append(f"Unknown finding ID: {fid}")
                else:
                    finding = findings_registry[fid]
                    if not finding.verified:
                        claim_has_unresolved = True
                        unresolved.append(f"Unverified finding ID: {fid}")

            if claim_has_unresolved:
                dropped_claims.append(claim)

    # A report with no cited claims would pass vacuously (PRD FR-5.4).
    if not any(section.claims for section in report.sections):
        unresolved.append("Report contains no cited claims")

    is_valid = len(unresolved) == 0
    details = (
        "All citations resolved successfully to verified findings."
        if is_valid
        else f"Found {len(unresolved)} citation resolution issues."
    )

    return ValidationResult(
        valid=is_valid,
        unresolved_citations=unresolved,
        missing_sources=missing_sources,
        dropped_claims=dropped_claims,
        details=details,
    )
