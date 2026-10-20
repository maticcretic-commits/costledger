"""Tests for CostLedger log parsing, pricing, rollups, budget, compare."""
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from costledger import (builtin_prices, build_ledger, compare_ledgers,
                        event_cost, extract_usage, get_pricing, load_pricing,
                        parse_log_text, parse_logs, parse_pricing_yaml,
                        rollup, unknown_models)

CLI = Path(__file__).parent.parent / "src" / "costledger.py"

PRICING_YAML = """\
as_of: 2026-10-20
currency: USD
models:
  model-a:
    input: 2.00
    output: 8.00
  model-b:
    input: 1.00
    output: 1.00
"""

PRICING_JSON = {
    "as_of": "2026-10-20", "currency": "USD",
    "models": {"model-a": {"input": 2.00, "output": 8.00},
               "model-b": {"input": 1.00, "output": 1.00}},
}


def prices(tmp_path, text=PRICING_YAML, name="pricing.yaml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return load_pricing(str(p))


def parse(text, run="runfile"):
    return parse_log_text(text, run, "mem.jsonl")


# ---------------------------------------------------------------------------
# Token dialects
# ---------------------------------------------------------------------------

def test_input_output_dialect():
    r = parse('{"model": "model-a", "input_tokens": 100, "output_tokens": 50}')
    assert len(r.events) == 1
    ev = r.events[0]
    assert (ev.input_tokens, ev.output_tokens) == (100, 50)


def test_prompt_completion_dialect():
    r = parse('{"model": "model-a", "prompt_tokens": 70, "completion_tokens": 30}')
    ev = r.events[0]
    assert (ev.input_tokens, ev.output_tokens) == (70, 30)


def test_total_tokens_fallback_counts_as_input():
    r = parse('{"model": "model-a", "total_tokens": 250}')
    ev = r.events[0]
    assert (ev.input_tokens, ev.output_tokens) == (250, 0)


def test_nested_usage_object():
    r = parse('{"model": "model-a", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}')
    ev = r.events[0]
    assert (ev.input_tokens, ev.output_tokens) == (10, 5)


def test_extract_usage_no_usage_returns_none():
    assert extract_usage({"event": "note"}) is None


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------

LOG = "\n".join([
    '{"run_id": "r1", "model": "model-a", "tool": "search", "input_tokens": 1000, "output_tokens": 100}',
    '{"run_id": "r1", "model": "model-a", "tool": "code", "input_tokens": 500, "output_tokens": 50}',
    '{"run_id": "r2", "model": "model-b", "tool": "search", "input_tokens": 2000, "output_tokens": 2000}',
    '{"session": "r2", "model": "model-b", "input_tokens": 10, "output_tokens": 10}',
])


def test_rollups_per_model_tool_run(tmp_path):
    roll = rollup(parse(LOG).events, prices(tmp_path))
    assert roll["by_model"]["model-a"].input_tokens == 1500
    assert roll["by_model"]["model-a"].events == 2
    assert roll["by_tool"]["search"].output_tokens == 2100
    assert roll["by_tool"]["(none)"].events == 1
    # "session" is a run_id alias → same bucket as run_id "r2"
    assert roll["by_run"]["r2"].events == 2
    assert roll["by_run"]["r1"].total_tokens == 1650


def test_exact_cost_math(tmp_path):
    p = prices(tmp_path)
    ev = parse('{"model": "model-a", "input_tokens": 1000, "output_tokens": 100}').events[0]
    # 1000*2/1M + 100*8/1M = 0.002 + 0.0008
    assert event_cost(ev, p) == Decimal("0.0028")


def test_run_id_defaults_to_file_stem():
    r = parse('{"model": "model-a", "input_tokens": 1}', run="mylog")
    assert r.events[0].run_id == "mylog"


def test_missing_model_bucketed_not_dropped():
    r = parse('{"input_tokens": 5, "output_tokens": 5}')
    assert len(r.events) == 1
    assert r.events[0].model == "(missing model)"


# ---------------------------------------------------------------------------
# Unknown models — tallied, never silently $0
# ---------------------------------------------------------------------------

def test_unknown_model_tokens_counted_cost_none(tmp_path):
    p = prices(tmp_path)
    r = parse('{"model": "mystery", "input_tokens": 999, "output_tokens": 1}')
    roll = rollup(r.events, p)
    b = roll["by_model"]["mystery"]
    assert b.total_tokens == 1000
    assert b.cost is None
    assert event_cost(r.events[0], p) is None
    assert unknown_models(r.events, p) == ["mystery"]
    assert roll["total"].complete is False
    assert roll["total"].known_cost == Decimal(0)


def test_ledger_marks_unknown_and_null_total(tmp_path):
    ledger = build_ledger(parse('{"model": "mystery", "input_tokens": 10}'),
                          ["x.jsonl"], prices(tmp_path))
    assert ledger["unknown_models"] == ["mystery"]
    assert ledger["totals"]["cost_usd"] is None
    assert ledger["totals"]["cost_complete"] is False


# ---------------------------------------------------------------------------
# Malformed / skipped lines, empty logs, JSON list files, directories
# ---------------------------------------------------------------------------

def test_malformed_lines_skipped_and_counted():
    text = "\n".join([
        '{"model": "model-a", "input_tokens": 10}',
        'this is not json',
        '{"event": "note", "message": "no usage here"}',
        '',
        '{"model": "model-a", "input_tokens": "lots"}',
    ])
    r = parse(text)
    assert len(r.events) == 1
    assert r.lines_malformed == 2      # bad JSON + unusable usage value
    assert r.lines_no_usage == 1      # the note line
    assert r.events_skipped == 3


def test_empty_log(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    parsed, sources = parse_logs(str(p))
    assert parsed.events == []
    ledger = build_ledger(parsed, sources, builtin_prices())
    assert ledger["totals"]["cost_usd"] == "0"
    assert ledger["events"] == 0


def test_json_list_file_input(tmp_path):
    p = tmp_path / "events.json"
    p.write_text(json.dumps([
        {"model": "model-a", "input_tokens": 100, "output_tokens": 10},
        {"model": "model-b", "total_tokens": 40},
        {"event": "note"},
    ]), encoding="utf-8")
    parsed, sources = parse_logs(str(p))
    assert len(parsed.events) == 2
    assert parsed.lines_no_usage == 1
    assert parsed.events[1].input_tokens == 40


def test_directory_input_aggregates(tmp_path):
    (tmp_path / "a.jsonl").write_text(
        '{"model": "model-a", "input_tokens": 100}', encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(
        '{"model": "model-a", "input_tokens": 200}', encoding="utf-8")
    parsed, sources = parse_logs(str(tmp_path))
    assert len(sources) == 2
    assert sum(e.input_tokens for e in parsed.events) == 300


# ---------------------------------------------------------------------------
# Pricing parsers
# ---------------------------------------------------------------------------

def test_yaml_subset_parser(tmp_path):
    p = prices(tmp_path)
    assert p.as_of == "2026-10-20"
    assert p.currency == "USD"
    assert p.models["model-a"] == (Decimal("2.00"), Decimal("8.00"))
    assert p.warnings == []


def test_yaml_flow_form_and_comments():
    models, as_of, currency = parse_pricing_yaml(
        "# comment line\n"
        "as_of: 2026-01-01\n"
        "models:\n"
        "  m1: {input: 0.50, output: 1.50}  # trailing comment\n")
    assert models["m1"]["input"] == Decimal("0.50")
    assert as_of == "2026-01-01"


def test_json_pricing_parser(tmp_path):
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps(PRICING_JSON), encoding="utf-8")
    t = load_pricing(str(p))
    assert t.models["model-b"] == (Decimal("1.00"), Decimal("1.00"))
    assert t.as_of == "2026-10-20"


def test_missing_as_of_warns(tmp_path):
    p = prices(tmp_path, text="models:\n  m:\n    input: 1\n    output: 2\n")
    assert p.as_of is None
    assert any("as_of" in w for w in p.warnings)


def test_pricing_missing_output_rejected(tmp_path):
    with pytest.raises(ValueError):
        prices(tmp_path, text="models:\n  m:\n    input: 1\n")


def test_builtin_table_has_as_of():
    t = builtin_prices()
    assert t.as_of == "2026-10-20"
    assert t.warnings == []
    assert "gpt-5" in t.models


# ---------------------------------------------------------------------------
# Decimal exactness
# ---------------------------------------------------------------------------

def test_decimal_exact_totals_many_small_events(tmp_path):
    # 10,000 events x 100 input tokens @ $0.10/1M == exactly $0.100000.
    # Float accumulation of 0.00001 drifts; Decimal must not.
    p = prices(tmp_path, text=(
        "as_of: 2026-10-20\nmodels:\n  cheap:\n"
        "    input: 0.10\n    output: 0.10\n"))
    lines = ['{"model": "cheap", "input_tokens": 100}'] * 10_000
    roll = rollup(parse("\n".join(lines)).events, p)
    assert roll["total"].cost == Decimal("0.1")
    ledger = build_ledger(parse("\n".join(lines)), ["x"], p)
    assert Decimal(ledger["totals"]["cost_usd"]) == Decimal("0.1")


def test_json_output_shape(tmp_path):
    ledger = build_ledger(
        parse('{"run_id": "r", "model": "model-a", "tool": "t", '
              '"input_tokens": 1000, "output_tokens": 100}'),
        ["x.jsonl"], prices(tmp_path))
    for key in ("tool", "version", "generated_at", "sources", "pricing",
                "events", "events_skipped", "unknown_models", "totals",
                "by_model", "by_tool", "by_run"):
        assert key in ledger
    # Money travels as an exact string, never a float.
    assert ledger["totals"]["cost_usd"] == "0.0028"
    assert isinstance(ledger["totals"]["cost_usd"], str)
    json.dumps(ledger)  # fully serializable


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def ledger_with(total, by_model=None):
    return {"totals": {"cost_usd": total},
            "by_model": by_model or {}, "by_tool": {}}


def test_compare_increase_beyond_threshold_regresses():
    result, regressed = compare_ledgers(
        ledger_with("100"), ledger_with("115"), Decimal("10"))
    assert regressed is True
    assert Decimal(result["pct_change"]) == Decimal("15")


def test_compare_increase_within_threshold_ok():
    _, regressed = compare_ledgers(
        ledger_with("100"), ledger_with("105"), Decimal("10"))
    assert regressed is False


def test_compare_decrease_ok():
    result, regressed = compare_ledgers(
        ledger_with("100"), ledger_with("80"), Decimal("10"))
    assert regressed is False
    assert Decimal(result["diff_usd"]) == Decimal("-20")


def test_compare_default_threshold_is_ten_percent():
    # +5% passes and +15% fails at the documented default of 10.
    _, ok = compare_ledgers(ledger_with("100"), ledger_with("105"),
                            Decimal("10"))
    _, bad = compare_ledgers(ledger_with("100"), ledger_with("115"),
                             Decimal("10"))
    assert ok is False and bad is True


def test_compare_per_model_diff():
    base = ledger_with("100", {"m": {"cost_usd": "40"}})
    new = ledger_with("100", {"m": {"cost_usd": "55"},
                              "n": {"cost_usd": "5"}})
    result, _ = compare_ledgers(base, new, Decimal("10"))
    rows = {r["key"]: r for r in result["by_model"]}
    assert Decimal(rows["m"]["diff"]) == Decimal("15")
    assert Decimal(rows["n"]["baseline"]) == Decimal("0")


def test_compare_unknown_total_refused():
    with pytest.raises(ValueError):
        compare_ledgers(ledger_with(None), ledger_with("5"), Decimal("10"))


def test_compare_zero_baseline():
    _, regressed = compare_ledgers(
        ledger_with("0"), ledger_with("5"), Decimal("10"))
    assert regressed is True
    _, regressed = compare_ledgers(
        ledger_with("0"), ledger_with("0"), Decimal("10"))
    assert regressed is False


# ---------------------------------------------------------------------------
# CLI exit codes (subprocess — the contract CI depends on)
# ---------------------------------------------------------------------------

def run_cli(*argv):
    return subprocess.run([sys.executable, str(CLI), *argv],
                          capture_output=True, text=True)


def write_log(tmp_path, lines, name="run.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_cli_budget_pass_and_fail(tmp_path):
    pricing = str(tmp_path / "pricing.yaml")
    Path(pricing).write_text(PRICING_YAML, encoding="utf-8")
    log = write_log(tmp_path, [
        '{"model": "model-a", "input_tokens": 1000, "output_tokens": 100}'])
    ok = run_cli("budget", log, "--pricing", pricing, "--max", "1.00")
    assert ok.returncode == 0
    over = run_cli("budget", log, "--pricing", pricing, "--max", "0.001")
    assert over.returncode == 1
    assert "OVER BUDGET" in over.stdout


def test_cli_budget_unknown_model_unverifiable(tmp_path):
    pricing = str(tmp_path / "pricing.yaml")
    Path(pricing).write_text(PRICING_YAML, encoding="utf-8")
    log = write_log(tmp_path, ['{"model": "mystery", "input_tokens": 10}'])
    r = run_cli("budget", log, "--pricing", pricing, "--max", "100")
    assert r.returncode == 1
    assert "UNVERIFIABLE" in r.stdout


def test_cli_tally_json_and_save(tmp_path):
    log = write_log(tmp_path, [
        '{"model": "model-a", "input_tokens": 1000, "output_tokens": 100}'])
    save = str(tmp_path / "ledger.json")
    r = run_cli("tally", log, "--format", "json", "--save", save)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["totals"]["input_tokens"] == 1000
    assert Path(save).is_file()


def test_cli_compare_exit_codes(tmp_path):
    base = tmp_path / "base.json"
    new = tmp_path / "new.json"
    base.write_text(json.dumps(ledger_with("100")), encoding="utf-8")
    new.write_text(json.dumps(ledger_with("130")), encoding="utf-8")
    assert run_cli("compare", str(base), str(new)).returncode == 1
    assert run_cli("compare", str(base), str(new),
                   "--threshold-pct", "50").returncode == 0
    assert run_cli("compare", str(base), str(new),
                   "--format", "json").returncode == 1


def test_cli_missing_path_is_usage_error(tmp_path):
    assert run_cli("tally", str(tmp_path / "nope.jsonl")).returncode == 2
    assert run_cli("prices", "--pricing",
                   str(tmp_path / "nope.yaml")).returncode == 2


def test_cli_prices_prints_as_of(tmp_path):
    r = run_cli("prices")
    assert r.returncode == 0
    assert "2026-10-20" in r.stdout
    assert "USD" in r.stdout


# ---------------------------------------------------------------------------
# Phase 1: version, JSON envelope, error envelope (CONVENTIONS.md)
# ---------------------------------------------------------------------------

def stderr_envelope(stderr):
    """Parse the JSON error-envelope line from stderr (the line after
    the human-readable `error: ...` line)."""
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("error: ")
    return json.loads(lines[-1])["error"]


def test_cli_tally_json_envelope(tmp_path):
    pricing = str(tmp_path / "pricing.yaml")
    Path(pricing).write_text(PRICING_YAML, encoding="utf-8")
    log = write_log(tmp_path, [
        '{"model": "model-a", "input_tokens": 1000, "output_tokens": 100}'])
    r = run_cli("tally", log, "--pricing", pricing, "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["tool"] == "costledger"
    assert out["version"] == "1.0.0"
    assert out["schema_version"] == 1
    assert "totals" in out
    assert out["totals"]["cost_usd"] == "0.0028"
    # build_ledger's internal ledger-format version stays untouched:
    # the semver envelope is applied by the CLI layer, not there.
    raw = build_ledger(parse('{"model": "model-a", "input_tokens": 1}'),
                       ["x"], prices(tmp_path))
    assert raw["version"] == 1


def test_cli_tally_save_carries_envelope(tmp_path):
    pricing = str(tmp_path / "pricing.yaml")
    Path(pricing).write_text(PRICING_YAML, encoding="utf-8")
    log = write_log(tmp_path, [
        '{"model": "model-a", "input_tokens": 1000, "output_tokens": 100}'])
    save = tmp_path / "ledger.json"
    r = run_cli("tally", log, "--pricing", pricing, "--save", str(save))
    assert r.returncode == 0
    saved = json.loads(save.read_text(encoding="utf-8"))
    assert saved["tool"] == "costledger"
    assert saved["version"] == "1.0.0"
    assert saved["schema_version"] == 1
    assert "totals" in saved


def test_cli_version_flag():
    r = run_cli("--version")
    assert r.returncode == 0
    assert r.stdout.strip() == "costledger 1.0.0"


def test_cli_missing_log_error_envelope(tmp_path):
    r = run_cli("tally", str(tmp_path / "nope.jsonl"))
    assert r.returncode == 2
    env = stderr_envelope(r.stderr)
    assert env["code"] == "file_not_found"
    assert "path not found" in env["message"]


def test_cli_compare_mixed_old_and_new_ledgers(tmp_path):
    old = tmp_path / "old.json"     # pre-envelope shape: no envelope keys
    new = tmp_path / "new.json"     # envelope shape written by tally --save
    old.write_text(json.dumps(ledger_with("100")), encoding="utf-8")
    new_ledger = ledger_with("105")
    new_ledger.update({"tool": "costledger", "version": "1.0.0",
                       "schema_version": 1})
    new.write_text(json.dumps(new_ledger), encoding="utf-8")
    r = run_cli("compare", str(old), str(new), "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["baseline_total_usd"] == "100"
    assert out["new_total_usd"] == "105"
    assert out["tool"] == "costledger"
    assert out["version"] == "1.0.0"
    assert out["schema_version"] == 1
    # And the other way around (envelope baseline, old-shape new).
    r2 = run_cli("compare", str(new), str(old))
    assert r2.returncode == 0


def test_cli_budget_bad_max_error_envelope(tmp_path):
    pricing = str(tmp_path / "pricing.yaml")
    Path(pricing).write_text(PRICING_YAML, encoding="utf-8")
    log = write_log(tmp_path, [
        '{"model": "model-a", "input_tokens": 1000, "output_tokens": 100}'])
    r = run_cli("budget", log, "--pricing", pricing, "--max", "abc")
    assert r.returncode == 2
    env = stderr_envelope(r.stderr)
    assert env["code"] == "invalid_number"
    assert "--max must be a number" in env["message"]


def test_cli_init_scaffolds(tmp_path):
    out = tmp_path / "scaffold"
    r = run_cli("init", "--dir", str(out))
    assert r.returncode == 0
    assert (out / "pricing.yaml").is_file()
    assert (out / "sample-run.jsonl").is_file()
    assert (out / "EXPECTED.md").is_file()
    # The scaffolded sample tallies with its own pricing file.
    t = run_cli("tally", str(out / "sample-run.jsonl"),
                "--pricing", str(out / "pricing.yaml"))
    assert t.returncode == 0
    assert "UNKNOWN" in t.stdout
