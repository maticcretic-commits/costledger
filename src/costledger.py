#!/usr/bin/env python3
"""
CostLedger — "Know what every agent run costs — before the invoice does."

AI agents burn tokens quietly. A runaway loop, a prompt that grew by
2,000 tokens, a model swap nobody priced — none of it shows up until
the provider invoice lands at the end of the month. CostLedger reads
the run logs you already have, tallies token usage per run, per model
and per tool against a local pricing table, gates budgets in CI, and
diffs cost against a saved baseline — the cost counterpart to
AgentCI's behavior regression.

Offline and dependency-free: logs are parsed locally, money is
Decimal-exact (never float), and an unknown model is reported as
UNKNOWN — never silently costed at $0.

Usage:
    costledger tally <log-or-dir> [--pricing pricing.yaml]
                        [--format text|json] [--save ledger.json]
    costledger budget <log-or-dir> --max <usd> [--pricing pricing.yaml]
    costledger compare <baseline-ledger.json> <new-ledger.json>
                        [--threshold-pct 10] [--format text|json]
    costledger prices [--pricing pricing.yaml]
    costledger init
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

VERSION = "1.0.0"  # tool version (distinct from the ledger format version)

PER_MILLION = Decimal(1_000_000)
DISPLAY_QUANT = Decimal("0.0001")
DEFAULT_COMPARE_THRESHOLD_PCT = Decimal("10")

BUILTIN_AS_OF = "2026-10-20"
# Approximate published list prices (USD per 1M tokens) as of the date
# above. They drift — verify current prices with your provider and pin
# your own pricing.yaml for anything you gate money on.
BUILTIN_PRICES = {
    "gpt-5": ("1.25", "10.00"),
    "gpt-5-mini": ("0.25", "2.00"),
    "gpt-4o": ("2.50", "10.00"),
    "claude-sonnet-4.5": ("3.00", "15.00"),
    "claude-opus-4.1": ("15.00", "75.00"),
    "claude-haiku-4.5": ("1.00", "5.00"),
    "gemini-2.5-pro": ("1.25", "10.00"),
    "gemini-2.5-flash": ("0.30", "2.50"),
    "llama-3.3-70b-instruct": ("0.59", "0.79"),
    "deepseek-chat": ("0.28", "0.42"),
}

MISSING_MODEL = "(missing model)"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PriceTable:
    models: dict = field(default_factory=dict)  # name -> (input, output) Decimal per 1M
    as_of: str = None          # "YYYY-MM-DD" or None
    currency: str = "USD"
    source: str = "built-in default table"
    warnings: list = field(default_factory=list)


@dataclass
class UsageEvent:
    model: str
    input_tokens: int
    output_tokens: int
    tool: str = None
    run_id: str = ""
    source: str = ""


@dataclass
class ParseResult:
    events: list = field(default_factory=list)
    lines_total: int = 0        # non-blank lines / list elements seen
    lines_no_usage: int = 0     # parsed fine, carried no token usage
    lines_malformed: int = 0    # not JSON, or usage fields unusable

    @property
    def events_skipped(self):
        return self.lines_no_usage + self.lines_malformed


@dataclass
class Bucket:
    events: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    known_cost: Decimal = Decimal(0)   # sum over events whose model is priced
    complete: bool = True              # False once an unknown-model event lands

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    @property
    def cost(self):
        """Exact Decimal cost, or None when any part is unpriced."""
        return self.known_cost if self.complete else None


# ---------------------------------------------------------------------------
# Pricing — built-in table, restricted-YAML subset parser, JSON parser
# ---------------------------------------------------------------------------

def builtin_prices():
    return PriceTable(
        models={name: (Decimal(i), Decimal(o))
                for name, (i, o) in BUILTIN_PRICES.items()},
        as_of=BUILTIN_AS_OF, currency="USD",
        source="built-in default table (approximate — verify current prices)")


def _decimal(text, what):
    try:
        return Decimal(str(text).strip().strip('"').strip("'"))
    except InvalidOperation:
        raise ValueError(f"invalid number for {what}: {text!r}")


def _finish_table(models, as_of, currency, source):
    table = PriceTable(models=models, as_of=as_of,
                       currency=currency or "USD", source=source)
    if not as_of:
        table.warnings.append(
            "pricing has no as_of date — prices may be stale; "
            "add `as_of: YYYY-MM-DD` to the pricing file")
    if not models:
        table.warnings.append("pricing table contains no models")
    return table


def parse_pricing_yaml(text):
    """Parse the restricted pricing.yaml subset:

        as_of: 2026-10-20
        currency: USD
        models:
          model-name:
            input: 1.25      # USD per 1M input tokens
            output: 10.00    # USD per 1M output tokens

    Also accepts the flow form `model-name: {input: 1.25, output: 10.00}`.
    Indentation is spaces; comments (# ...) and blank lines are ignored.
    Raises ValueError on anything outside the subset."""
    as_of, currency, models = None, None, {}
    in_models = False
    current = None
    for raw in text.splitlines():
        # Strip full-line and trailing comments (# never appears in a
        # model name or a number in this subset).
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if indent == 0:
            key, sep, val = content.partition(":")
            if not sep:
                raise ValueError(f"cannot parse pricing line: {raw!r}")
            key, val = key.strip(), val.strip()
            if key == "models":
                in_models, current = True, None
            elif key == "as_of":
                as_of = val.strip('"').strip("'") or None
                in_models = False
            elif key == "currency":
                currency = val.strip('"').strip("'") or None
                in_models = False
            else:
                in_models = False
        elif in_models and indent == 2:
            if content.endswith(":"):
                current = content[:-1].strip().strip('"').strip("'")
                models.setdefault(current, {})
            else:
                name, _, val = content.partition(":")
                current = name.strip().strip('"').strip("'")
                models.setdefault(current, {})
                val = val.strip()
                if val.startswith("{") and val.endswith("}"):
                    for pair in val[1:-1].split(","):
                        k, _, v = pair.partition(":")
                        k = k.strip()
                        if k in ("input", "output"):
                            models[current][k] = _decimal(v, f"{current}.{k}")
                elif val:
                    raise ValueError(f"cannot parse pricing line: {raw!r}")
        elif in_models and indent >= 4 and current is not None:
            k, sep, v = content.partition(":")
            if not sep:
                raise ValueError(f"cannot parse pricing line: {raw!r}")
            k = k.strip()
            if k in ("input", "output"):
                models[current][k] = _decimal(v, f"{current}.{k}")
        else:
            raise ValueError(f"cannot parse pricing line: {raw!r}")
    return models, as_of, currency


def parse_pricing_data(data):
    """Build a model price map from parsed JSON pricing data."""
    models = {}
    for name, entry in (data.get("models") or {}).items():
        if isinstance(entry, dict):
            models[name] = {
                "input": _decimal(entry.get("input", 0), f"{name}.input"),
                "output": _decimal(entry.get("output", 0), f"{name}.output"),
            }
        else:
            raise ValueError(f"pricing entry for {name!r} must be an object")
    return models


def _validate_models(models, source):
    out = {}
    for name, entry in models.items():
        if "input" not in entry or "output" not in entry:
            raise ValueError(
                f"pricing for {name!r} in {source} needs both "
                f"'input' and 'output' (USD per 1M tokens)")
        out[name] = (entry["input"], entry["output"])
    return out


def load_pricing(path):
    """Load a pricing file (.yaml/.yml subset, or .json). Raises
    ValueError on unreadable or malformed input."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"pricing file not found: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON in pricing file {path}: {e}")
        if not isinstance(data, dict):
            raise ValueError(f"pricing file {path} must contain a JSON object")
        models = parse_pricing_data(data)
        as_of = data.get("as_of") or None
        currency = data.get("currency") or None
    else:
        models, as_of, currency = parse_pricing_yaml(text)
    return _finish_table(_validate_models(models, str(path)),
                         as_of, currency, str(path))


def get_pricing(path):
    if path:
        return load_pricing(path)
    return builtin_prices()


# ---------------------------------------------------------------------------
# Log parsing — JSONL events, or a single JSON list/object per file
# ---------------------------------------------------------------------------

def _as_count(value):
    """Coerce a token count to a non-negative int, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value == int(value) else None
    if isinstance(value, str) and value.strip().lstrip("+").isdigit():
        return int(value.strip())
    return None


def extract_usage(obj):
    """Pull (input_tokens, output_tokens) out of one event object, in
    any of the documented dialects. Returns None when the object
    carries no usable usage. A nested "usage" object is also read."""
    scopes = [obj]
    nested = obj.get("usage")
    if isinstance(nested, dict):
        scopes.append(nested)
    for scope in scopes:
        for in_key, out_key in (("input_tokens", "output_tokens"),
                                ("prompt_tokens", "completion_tokens")):
            if in_key in scope or out_key in scope:
                i = _as_count(scope.get(in_key, 0))
                o = _as_count(scope.get(out_key, 0))
                if i is None or o is None:
                    return "malformed"
                return (i, o)
        if "total_tokens" in scope:
            t = _as_count(scope.get("total_tokens"))
            if t is None:
                return "malformed"
            # Split unknown — count it all as input, per the format doc.
            return (t, 0)
    return None


def event_from_obj(obj, default_run, source):
    """Build a UsageEvent from a parsed JSON object. Returns one of:
    a UsageEvent, "no-usage", or "malformed"."""
    if not isinstance(obj, dict):
        return "no-usage"
    usage = extract_usage(obj)
    if usage is None:
        return "no-usage"
    if usage == "malformed":
        return "malformed"
    i, o = usage
    model = obj.get("model")
    if not isinstance(model, str) or not model.strip():
        nested = obj.get("usage")
        model = nested.get("model") if isinstance(nested, dict) else None
    if not isinstance(model, str) or not model.strip():
        model = MISSING_MODEL
    run_id = obj.get("run_id") or obj.get("session") or default_run
    tool = obj.get("tool")
    return UsageEvent(model=model.strip(), input_tokens=i, output_tokens=o,
                      tool=tool if isinstance(tool, str) and tool else None,
                      run_id=str(run_id), source=source)


def parse_log_text(text, default_run, source):
    """Parse one file's content: a whole-file JSON list/object when it
    parses as one, otherwise JSONL (one event per line)."""
    result = ParseResult()
    stripped = text.strip()
    if not stripped:
        return result
    whole = None
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    items = None
    if isinstance(whole, list):
        items = whole
    elif isinstance(whole, dict):
        items = [whole]
    if items is not None:
        for obj in items:
            result.lines_total += 1
            _absorb(result, event_from_obj(obj, default_run, source))
        return result
    for line in text.splitlines():
        if not line.strip():
            continue
        result.lines_total += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            result.lines_malformed += 1
            continue
        _absorb(result, event_from_obj(obj, default_run, source))
    return result


def _absorb(result, made):
    if isinstance(made, UsageEvent):
        result.events.append(made)
    elif made == "malformed":
        result.lines_malformed += 1
    else:
        result.lines_no_usage += 1


def is_binary_bytes(data):
    return b"\x00" in data[:8192]


def iter_log_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            yield p


def parse_logs(path):
    """Parse a log file or a directory of logs. Returns (ParseResult,
    [source labels]). Raises FileNotFoundError for a missing path."""
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"path not found: {path}")
    merged = ParseResult()
    sources = []
    for f in iter_log_files(root):
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if is_binary_bytes(data):
            continue
        sources.append(str(f))
        part = parse_log_text(data.decode("utf-8", errors="replace"),
                              f.stem, str(f))
        merged.events.extend(part.events)
        merged.lines_total += part.lines_total
        merged.lines_no_usage += part.lines_no_usage
        merged.lines_malformed += part.lines_malformed
    return merged, sources


# ---------------------------------------------------------------------------
# Tally — Decimal-exact rollups per run / model / tool
# ---------------------------------------------------------------------------

def event_cost(event, prices):
    """Exact Decimal cost of one event, or None when its model has no
    price (UNKNOWN — never silently $0)."""
    entry = prices.models.get(event.model)
    if entry is None:
        return None
    input_price, output_price = entry
    return (Decimal(event.input_tokens) * input_price
            + Decimal(event.output_tokens) * output_price) / PER_MILLION


def _add(bucket, event, cost):
    bucket.events += 1
    bucket.input_tokens += event.input_tokens
    bucket.output_tokens += event.output_tokens
    if cost is None:
        bucket.complete = False
    else:
        bucket.known_cost += cost


def rollup(events, prices):
    total, by_model, by_tool, by_run = Bucket(), {}, {}, {}
    for ev in events:
        cost = event_cost(ev, prices)
        _add(total, ev, cost)
        _add(by_model.setdefault(ev.model, Bucket()), ev, cost)
        _add(by_tool.setdefault(ev.tool or "(none)", Bucket()), ev, cost)
        _add(by_run.setdefault(ev.run_id, Bucket()), ev, cost)
    return {"total": total, "by_model": by_model,
            "by_tool": by_tool, "by_run": by_run}


def unknown_models(events, prices):
    """Sorted names of models in the events that have no price."""
    return sorted({ev.model for ev in events if ev.model not in prices.models})


# ---------------------------------------------------------------------------
# Ledger (JSON) shape — what --save writes and compare consumes
# ---------------------------------------------------------------------------

def money_str(value):
    """Exact Decimal as a plain JSON string (no float rounding)."""
    if value is None:
        return None
    return format(value, "f")


def bucket_json(bucket):
    return {"events": bucket.events,
            "input_tokens": bucket.input_tokens,
            "output_tokens": bucket.output_tokens,
            "total_tokens": bucket.total_tokens,
            "cost_usd": money_str(bucket.cost),
            "cost_complete": bucket.complete}


def build_ledger(parsed, sources, prices):
    roll = rollup(parsed.events, prices)
    total = roll["total"]
    return {
        "tool": "costledger",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
        "pricing": {"as_of": prices.as_of, "currency": prices.currency,
                    "source": prices.source},
        "pricing_warnings": list(prices.warnings),
        "events": len(parsed.events),
        "events_skipped": parsed.events_skipped,
        "lines_malformed": parsed.lines_malformed,
        "unknown_models": unknown_models(parsed.events, prices),
        "totals": bucket_json(total),
        "by_model": {k: bucket_json(b)
                     for k, b in sorted(roll["by_model"].items())},
        "by_tool": {k: bucket_json(b)
                    for k, b in sorted(roll["by_tool"].items())},
        "by_run": {k: bucket_json(b)
                   for k, b in sorted(roll["by_run"].items())},
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_money(value):
    """Display form: $X.XXXX (4dp, half-up). None → 'UNKNOWN'."""
    if value is None:
        return "UNKNOWN"
    q = value.quantize(DISPLAY_QUANT, rounding=ROUND_HALF_UP)
    return f"${q}"


def fmt_tokens(n):
    return f"{n:,}"


def print_warnings(prices):
    for w in prices.warnings:
        print(f"⚠️  WARNING: {w}")


def _bucket_lines(title, buckets):
    print(f"{title}:")
    if not buckets:
        print("  (none)")
    for name in sorted(buckets):
        b = buckets[name]
        cost = fmt_money(b.cost)
        if not b.complete and b.known_cost:
            cost += f" (known part only; total ≥ {fmt_money(b.known_cost)})"
        print(f"  {name:<28} {fmt_tokens(b.input_tokens):>10} in / "
              f"{fmt_tokens(b.output_tokens):>10} out   {cost}")
    print()


def print_tally_text(ledger, roll, parsed, prices):
    print("=" * 68)
    print("CostLedger — tally report")
    print("=" * 68)
    print(f"Sources: {', '.join(ledger['sources']) or '(none)'}")
    print(f"Pricing: {prices.source} "
          f"(as of {prices.as_of or 'UNKNOWN'}, {prices.currency})")
    print()
    print_warnings(prices)
    _bucket_lines("Per run", roll["by_run"])
    _bucket_lines("Per model", roll["by_model"])
    _bucket_lines("Per tool", roll["by_tool"])
    total = roll["total"]
    print(f"Events: {len(parsed.events)} used · "
          f"{parsed.events_skipped} skipped "
          f"({parsed.lines_malformed} malformed)")
    print(f"Tokens: {fmt_tokens(total.input_tokens)} in / "
          f"{fmt_tokens(total.output_tokens)} out "
          f"({fmt_tokens(total.total_tokens)} total)")
    if ledger["unknown_models"]:
        names = ", ".join(ledger["unknown_models"])
        print(f"⚠️  UNKNOWN MODEL(S): {names} — tokens tallied above, "
              f"cost UNKNOWN (not $0). Add prices for them to price "
              f"these runs.")
    if total.complete:
        print(f"Total cost: {fmt_money(total.cost)}")
    else:
        print(f"Total cost: UNKNOWN — known part is "
              f"{fmt_money(total.known_cost)}; the true total is higher.")


# ---------------------------------------------------------------------------
# Compare — cost regression against a saved baseline ledger
# ---------------------------------------------------------------------------

def ledger_total(ledger):
    raw = (ledger.get("totals") or {}).get("cost_usd")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def diff_buckets(base, new):
    """Per-key cost diff between two by_* maps. Costs are Decimals or
    None (unknown). Returns rows sorted by key."""
    rows = []
    for key in sorted(set(base) | set(new)):
        b = _bucket_cost(base.get(key))
        n = _bucket_cost(new.get(key))
        diff = None
        if b is not None and n is not None:
            diff = n - b
        rows.append({"key": key, "baseline": money_str(b),
                     "new": money_str(n), "diff": money_str(diff)})
    return rows


def _bucket_cost(bucket):
    if not bucket:
        return Decimal(0)
    raw = bucket.get("cost_usd")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def compare_ledgers(base, new, threshold_pct):
    """Returns (result dict, regressed bool). Raises ValueError when a
    ledger's total is unknown — a diff against an unpriced baseline
    would be a guess."""
    base_total = ledger_total(base)
    new_total = ledger_total(new)
    if base_total is None or new_total is None:
        raise ValueError(
            "cannot compare: a ledger total is UNKNOWN (it contains "
            "unpriced models) — price every model, re-tally, then compare")
    diff = new_total - base_total
    if base_total == 0:
        pct = None  # undefined; any positive new total is a regression
        regressed = new_total > 0
    else:
        pct = (diff / base_total) * 100
        regressed = pct > threshold_pct
    result = {
        "baseline_total_usd": money_str(base_total),
        "new_total_usd": money_str(new_total),
        "diff_usd": money_str(diff),
        "pct_change": None if pct is None else format(pct, "f"),
        "threshold_pct": format(threshold_pct, "f"),
        "regressed": regressed,
        "by_model": diff_buckets(base.get("by_model") or {},
                                 new.get("by_model") or {}),
        "by_tool": diff_buckets(base.get("by_tool") or {},
                                new.get("by_tool") or {}),
    }
    return result, regressed


def print_compare_text(result):
    print("=" * 68)
    verdict = "❌ COST REGRESSION" if result["regressed"] else "✅ no cost regression"
    print(f"CostLedger compare — {verdict}")
    print("=" * 68)
    print(f"Baseline total: ${result['baseline_total_usd']}")
    print(f"New total:      ${result['new_total_usd']}")
    pct = result["pct_change"]
    pct_txt = "n/a (baseline was $0)" if pct is None else f"{Decimal(pct):+.2f}%"
    print(f"Change:         ${result['diff_usd']} ({pct_txt}) "
          f"· threshold {result['threshold_pct']}%")
    for title, rows in (("Per model", result["by_model"]),
                        ("Per tool", result["by_tool"])):
        print()
        print(f"{title}:")
        for row in rows:
            if row["diff"] is None:
                print(f"  {row['key']:<28} UNKNOWN (unpriced in one ledger)")
            else:
                d = Decimal(row["diff"])
                arrow = "🔺" if d > 0 else ("🔻" if d < 0 else "—")
                print(f"  {row['key']:<28} ${row['baseline']} → "
                      f"${row['new']}  ({arrow} ${row['diff']})")


def load_ledger(path):
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"ledger not found: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in ledger {path}: {e}")
    if not isinstance(data, dict) or "totals" not in data:
        raise ValueError(f"{path} is not a CostLedger ledger "
                         f"(write one with: costledger tally --save)")
    return data


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _error_exit(message, code):
    """Caller error (exit 2): print the human-readable line to stderr,
    then the JSON error envelope (CONVENTIONS.md), then exit 2."""
    print(f"error: {message}", file=sys.stderr)
    print(json.dumps({"error": {"code": code, "message": message}}),
          file=sys.stderr)
    sys.exit(2)


def _load_or_exit(path):
    try:
        return parse_logs(path)
    except FileNotFoundError as e:
        _error_exit(str(e), "file_not_found")


def _pricing_or_exit(path):
    try:
        return get_pricing(path)
    except ValueError as e:
        _error_exit(str(e), "invalid_pricing")


def cmd_tally(args):
    parsed, sources = _load_or_exit(args.path)
    prices = _pricing_or_exit(args.pricing)
    ledger = build_ledger(parsed, sources, prices)
    # JSON envelope (CONVENTIONS.md): tool version + schema version on
    # every emitted/saved ledger. (build_ledger's own "version": 1 is
    # the ledger format version and stays untouched there.)
    ledger["tool"] = "costledger"
    ledger["version"] = VERSION
    ledger["schema_version"] = 1
    if args.save:
        Path(args.save).write_text(json.dumps(ledger, indent=2),
                                   encoding="utf-8")
    if args.format == "json":
        print(json.dumps(ledger, indent=2))
    else:
        roll = rollup(parsed.events, prices)
        print_tally_text(ledger, roll, parsed, prices)
        if args.save:
            print(f"\nLedger saved to {args.save}")
    sys.exit(0)


def cmd_budget(args):
    parsed, sources = _load_or_exit(args.path)
    prices = _pricing_or_exit(args.pricing)
    roll = rollup(parsed.events, prices)
    total = roll["total"]
    try:
        limit = Decimal(str(args.max))
    except InvalidOperation:
        _error_exit(f"--max must be a number, got {args.max!r}",
                    "invalid_number")
    print_warnings(prices)
    unknown = unknown_models(parsed.events, prices)
    if total.known_cost > limit:
        print(f"❌ OVER BUDGET — total cost {fmt_money(total.known_cost)} "
              f"exceeds budget {fmt_money(limit)}")
        sys.exit(1)
    if unknown:
        names = ", ".join(unknown)
        print(f"❌ BUDGET UNVERIFIABLE — unknown model(s): {names}. "
              f"Known cost is {fmt_money(total.known_cost)} of budget "
              f"{fmt_money(limit)}, but unpriced usage can only push "
              f"the true total up. Add prices and re-run.")
        sys.exit(1)
    print(f"✅ WITHIN BUDGET — total cost {fmt_money(total.cost)} "
          f"of budget {fmt_money(limit)}")
    sys.exit(0)


def cmd_compare(args):
    try:
        base = load_ledger(args.baseline)
        new = load_ledger(args.new)
        threshold = Decimal(str(args.threshold_pct))
    except (ValueError, InvalidOperation) as e:
        msg = str(e)
        if msg.startswith("ledger not found"):
            code = "file_not_found"
        elif "invalid JSON" in msg:
            code = "invalid_json"
        else:
            code = "invalid_input"
        _error_exit(msg, code)
    try:
        result, regressed = compare_ledgers(base, new, threshold)
    except ValueError as e:
        _error_exit(str(e), "invalid_input")
    # JSON envelope (CONVENTIONS.md).
    result["tool"] = "costledger"
    result["version"] = VERSION
    result["schema_version"] = 1
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print_compare_text(result)
    # 0 = within threshold, 1 = cost regression, 2 = input error.
    sys.exit(1 if regressed else 0)


def cmd_prices(args):
    prices = _pricing_or_exit(args.pricing)
    print("=" * 68)
    print("CostLedger — pricing table")
    print("=" * 68)
    print(f"Source:   {prices.source}")
    print(f"As of:    {prices.as_of or 'UNKNOWN — no as_of date'}")
    print(f"Currency: {prices.currency}")
    print()
    print_warnings(prices)
    print(f"  {'model':<28} {'input $/1M':>12} {'output $/1M':>12}")
    for name in sorted(prices.models):
        i, o = prices.models[name]
        print(f"  {name:<28} {format(i, 'f'):>12} {format(o, 'f'):>12}")
    if prices.source.startswith("built-in"):
        print("\nBuilt-in prices are approximate list prices — pin your "
              "own pricing.yaml for anything you gate money on.")
    sys.exit(0)


PRICING_TEMPLATE = """\
# CostLedger pricing — USD per 1M tokens. Keep as_of current.
as_of: 2026-10-20
currency: USD
models:
  gpt-5:
    input: 1.25
    output: 10.00
  claude-sonnet-4.5:
    input: 3.00
    output: 15.00
"""

SAMPLE_LOG_TEMPLATE = """\
{"run_id": "run-alpha", "model": "gpt-5", "tool": "planner", "input_tokens": 1200, "output_tokens": 300, "timestamp": "2026-10-20T09:15:00Z"}
{"run_id": "run-alpha", "model": "gpt-5", "tool": "web-search", "prompt_tokens": 800, "completion_tokens": 200, "timestamp": "2026-10-20T09:15:04Z"}
{"run_id": "run-alpha", "model": "claude-sonnet-4.5", "tool": "coder", "input_tokens": 2000, "output_tokens": 900, "timestamp": "2026-10-20T09:16:41Z"}
{"run_id": "run-beta", "model": "gpt-5", "tool": "coder", "total_tokens": 1500, "timestamp": "2026-10-20T10:02:10Z"}
{"run_id": "run-beta", "model": "mystery-model-9000", "tool": "coder", "input_tokens": 640, "output_tokens": 128, "timestamp": "2026-10-20T10:03:55Z"}
{"run_id": "run-beta", "event": "note", "message": "checkpoint saved — no usage on this line"}
"""

EXPECTED_TEMPLATE = """\
# What you should see

    costledger tally sample-run.jsonl --pricing pricing.yaml

- Per model: gpt-5 = 3,500 in / 500 out = $0.0094 · claude-sonnet-4.5 =
  2,000 in / 900 out = $0.0195 · mystery-model-9000 = UNKNOWN.
- Totals: 5 events used, 1 skipped (the note line), 6,140 in / 1,528 out.
- The report flags mystery-model-9000 loudly and the total reads
  "UNKNOWN — known part is $0.0289".

    costledger budget sample-run.jsonl --pricing pricing.yaml --max 0.05

- Exits 1: not because $0.0289 of known cost exceeds $0.05 (it doesn't),
  but because the unknown model makes the true total unverifiable.

Remove the mystery-model line (or add a price for it) and the same
budget passes with exit 0.
"""


def cmd_init(args):
    base = Path(args.dir) if args.dir else Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    targets = {"pricing.yaml": PRICING_TEMPLATE,
               "sample-run.jsonl": SAMPLE_LOG_TEMPLATE,
               "EXPECTED.md": EXPECTED_TEMPLATE}
    for name, content in targets.items():
        p = base / name
        if p.exists() and not args.force:
            print(f"{p} exists — use --force to overwrite")
            return
        p.write_text(content, encoding="utf-8")
    print(f"created pricing.yaml, sample-run.jsonl and EXPECTED.md in {base}")
    print("try it:  costledger tally sample-run.jsonl --pricing pricing.yaml")


def main():
    ap = argparse.ArgumentParser(
        prog="costledger",
        description="Offline cost accounting for AI agent runs. "
                    "Know what every agent run costs — before the "
                    "invoice does.")
    ap.add_argument("--version", action="version",
                    version=f"costledger {VERSION}")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color output (this tool emits "
                         "no color; accepted for family consistency)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tally", help="tally token usage + cost from run logs")
    t.add_argument("path", help="log file or directory of logs")
    t.add_argument("--pricing", help="pricing.yaml / pricing.json "
                                     "(default: built-in table)")
    t.add_argument("--format", choices=("text", "json"), default="text")
    t.add_argument("--save", metavar="LEDGER",
                   help="also write the ledger JSON (input for compare)")
    t.set_defaults(fn=cmd_tally)

    b = sub.add_parser("budget", help="fail (exit 1) when cost exceeds a budget")
    b.add_argument("path", help="log file or directory of logs")
    b.add_argument("--max", required=True, metavar="USD",
                   help="budget ceiling in USD, e.g. --max 5.00")
    b.add_argument("--pricing", help="pricing.yaml / pricing.json")
    b.set_defaults(fn=cmd_budget)

    c = sub.add_parser("compare", help="diff two ledgers; exit 1 on cost regression")
    c.add_argument("baseline", help="baseline ledger JSON (tally --save)")
    c.add_argument("new", help="new ledger JSON (tally --save)")
    c.add_argument("--threshold-pct", default="10", metavar="PCT",
                   help="regression threshold in percent (default: 10)")
    c.add_argument("--format", choices=("text", "json"), default="text")
    c.set_defaults(fn=cmd_compare)

    p = sub.add_parser("prices", help="print the effective pricing table")
    p.add_argument("--pricing", help="pricing.yaml / pricing.json")
    p.set_defaults(fn=cmd_prices)

    i = sub.add_parser("init", help="scaffold pricing.yaml + sample log + EXPECTED.md")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
