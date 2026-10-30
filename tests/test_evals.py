from evals.run_evals import load_cases, run


def test_cases_file_is_valid():
    cases = load_cases()
    assert cases, "evals/cases.jsonl vazio"
    assert {c["split"] for c in cases} >= {"official"}


def test_gate_passes_in_process():
    report = run()
    assert report["passed"], report["failures"]
