"""Deterministic Code-Based Markdown Renderer (PRD FR-5.4).

Produces formatted Markdown directly from validated structure:
- Title and Executive Summary
- Sections with sequential numbered citations [1], [2]
- Coverage Gaps section
- Sequentially ordered References list with Title, URL, Retrieval Date, and Verbatim Snippet Quote
"""

from __future__ import annotations

from typing import Dict, List, Optional

from scout.schemas import Claim, Finding, ReportSection


def render_markdown_report(
    title: str,
    summary: str,
    sections: List[ReportSection],
    findings_registry: Dict[str, Finding],
    coverage_gaps: Optional[List[str]] = None,
    dropped_claims: Optional[List[Claim]] = None,
) -> str:
    """Renders structured report content into Markdown with verified citations and bibliography."""
    lines: List[str] = [
        f"# {title.strip()}",
        "",
        f"**Executive Summary:** {summary.strip()}",
        "",
    ]

    # Map finding_ids to sequential citation numbers [1], [2], ...
    citation_map: Dict[str, int] = {}
    next_num = 1

    for section in sections:
        lines.append(f"## {section.title.strip()}")
        lines.append("")
        if section.content:
            lines.append(section.content.strip())
            lines.append("")

        if section.claims:
            lines.append("**Key Supported Claims:**")
            for claim in section.claims:
                cit_labels: List[str] = []
                for fid in claim.finding_ids:
                    # Only map verified findings present in the registry
                    if fid in findings_registry and findings_registry[fid].verified:
                        if fid not in citation_map:
                            citation_map[fid] = next_num
                            next_num += 1
                        cit_labels.append(f"[{citation_map[fid]}]")

                cit_suffix = "".join(cit_labels) if cit_labels else ""
                lines.append(f"- {claim.text.strip()} {cit_suffix}".rstrip())
            lines.append("")

    # Coverage Gaps and Flagged Dropped Claims
    gaps: List[str] = list(coverage_gaps or [])
    if dropped_claims:
        for dc in dropped_claims:
            gaps.append(f"Unverified claim omitted: \"{dc.text[:60]}...\" (citation unresolved)")

    if gaps:
        lines.append("## Coverage Gaps")
        lines.append("The following topics or claims lacked verified evidence in the findings registry:")
        for gap in gaps:
            lines.append(f"- {gap.strip()}")
        lines.append("")

    # References / Bibliography
    # Each reference is its own block (blank-line separated) so renderers don't merge them into one paragraph
    lines.append("## References")
    lines.append("")
    if citation_map:
        sorted_citations = sorted(citation_map.items(), key=lambda x: x[1])
        for fid, num in sorted_citations:
            finding = findings_registry.get(fid)
            if finding:
                source_title = finding.title or finding.domain or "Web Source"
                retrieved_str = (
                    finding.retrieved_at.strftime("%Y-%m-%d %H:%M UTC")
                    if hasattr(finding.retrieved_at, "strftime")
                    else str(finding.retrieved_at)
                )
                lines.append(
                    f"[{num}] **{source_title}** - [{finding.source_url}]({finding.source_url}) "
                    f"(Retrieved {retrieved_str})"
                )
                lines.append(f"> *Quote: \"{finding.snippet}\"*")
            else:
                lines.append(f"[{num}] Finding ID: {fid}")
            lines.append("")
    else:
        lines.append("No citations recorded.")

    return "\n".join(lines).rstrip()
