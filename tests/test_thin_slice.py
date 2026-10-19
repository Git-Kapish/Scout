from scripts.thin_slice import run_thin_slice


def test_thin_slice_execution_and_gate_pass():
    results = run_thin_slice()
    assert results["provenance_rate"] >= 90.0
    assert results["fetch_rate"] >= 60.0
    assert results["citation_rate"] == 100.0
    assert results["useful_reports"] >= 3
    assert len(results["reports"]) == 5

    # Check each generated report has valid structure
    for report in results["reports"]:
        assert report.title != ""
        assert len(report.sections) >= 1
        assert report.markdown is not None
        assert "## References" in report.markdown
