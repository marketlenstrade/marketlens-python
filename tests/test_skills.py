"""The agent skills under skills/ name real SDK calls.

An agent follows a skill literally, so a wrong parameter or attribute name
there fails on a user's machine. These checks tie every name a skill uses to
the code it describes.
"""
import ast
import inspect
import re
from pathlib import Path

import pytest

import marketlens.exceptions as exceptions
from marketlens import MarketLens
from marketlens.backtest import (
    AlphaContext, AlphaStrategy, BacktestResult, Bar, Fill, Position, Strategy,
    StrategyContext,
)
from marketlens.resources.exports import Exports, SeriesDownloadResult
from marketlens.types.candle import Candle
from marketlens.types.market import Market
from marketlens.types.orderbook import BookMetrics, OrderBook
from marketlens.types.trade import Trade

SKILLS = sorted((Path(__file__).parent.parent / "skills").glob("*/SKILL.md"))


def _blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, re.S)


def _members(cls) -> set[str]:
    fields = set(getattr(cls, "model_fields", {})) | set(getattr(cls, "__dataclass_fields__", {}))
    return fields | {n for n in dir(cls) if not n.startswith("_")}


def _result_members() -> set[str]:
    src = inspect.getsource(BacktestResult)
    return _members(BacktestResult) | set(re.findall(r"self\.([a-z_]+)\s*[:=]", src))


def _used(text: str, obj: str) -> set[str]:
    """Attribute names read off ``obj`` anywhere in the skill, prose included."""
    return set(re.findall(rf"\b{obj}\.([a-z_]+)", text))


@pytest.fixture(params=SKILLS, ids=lambda p: p.parent.name)
def skill(request) -> str:
    return request.param.read_text()


def test_skills_exist():
    assert {p.parent.name for p in SKILLS} == {"polymarket-backtest", "polymarket-historical-data"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_frontmatter(path):
    text = path.read_text()
    front = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert front, "SKILL.md must open with YAML frontmatter"
    fields = dict(line.split(": ", 1) for line in front.group(1).splitlines())
    assert fields["name"] == path.parent.name
    assert 0 < len(fields["description"]) <= 1024
    assert "—" not in text and "–" not in text


def test_code_blocks_parse(skill):
    blocks = _blocks(skill)
    assert blocks
    for block in blocks:
        ast.parse(block)


def test_backtest_keywords_exist(skill):
    allowed = set(inspect.signature(MarketLens.backtest).parameters) | {"subtype"}
    for block in _blocks(skill):
        for node in ast.walk(ast.parse(block)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "backtest"
            ):
                unknown = {k.arg for k in node.keywords if k.arg} - allowed
                assert not unknown, f"client.backtest() has no parameter {unknown}"
    # Options the prose names as keyword=default.
    for name in re.findall(r"`([a-z_]+)=[^`]*`", skill):
        if name in {"take", "limit", "q", "sort", "category", "subtype", "at", "depth",
                    "after", "before", "size", "levels", "dry_run", "coalesce"}:
            continue
        assert name in allowed, f"`{name}=` is not a client.backtest() parameter"


def test_export_keywords_exist(skill):
    allowed = {
        "download": set(inspect.signature(Exports.download).parameters),
        "download_series": set(inspect.signature(Exports.download_series).parameters),
    }
    for block in _blocks(skill):
        for node in ast.walk(ast.parse(block)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in allowed
            ):
                unknown = {k.arg for k in node.keywords if k.arg} - allowed[node.func.attr]
                assert not unknown, f"exports.{node.func.attr}() has no parameter {unknown}"


def test_attributes_exist(skill):
    checks = {
        "ctx": _members(StrategyContext) | _members(AlphaContext),
        "book": _members(OrderBook),
        "bar": _members(Bar),
        "fill": _members(Fill),
        "result": _result_members(),
        "quote": _members(SeriesDownloadResult),
        "data": _members(SeriesDownloadResult),
        "m": _members(Market),
        "market": _members(Market),
        "trade": {"t", "id", "price", "size", "side"},
    }
    for obj, known in checks.items():
        unknown = _used(skill, obj) - known
        assert not unknown, f"{obj} has no attribute {unknown}"


def test_hooks_exist(skill):
    known = _members(Strategy) | _members(AlphaStrategy)
    hooks = set(re.findall(r"\b(on_[a-z_]+)\(", skill))
    assert hooks <= known, hooks - known


def test_exceptions_exist(skill):
    for name in set(re.findall(r"`([A-Z][A-Za-z]+Error)`", skill)):
        assert hasattr(exceptions, name) or name in {"ValueError"}, name


def test_dataframe_columns_exist(skill):
    """Column lists the data skill prints next to each to_dataframe() call."""
    for model, marker in ((Candle, "open_time; columns"), (BookMetrics, "index t; columns"),
                          (Trade, "platform_timestamp; columns")):
        line = next((ln for ln in skill.splitlines() if marker in ln), None)
        if line is None:
            continue
        names = set(re.findall(r"[a-z_]+", line.split("columns", 1)[1])) - {"columns"}
        assert names <= _members(model), names - _members(model)


def test_position_fields_exist(skill):
    line = next((ln for ln in skill.splitlines() if "ctx.position()` returns" in ln), None)
    if line is None:
        return
    names = set(re.findall(r"`([a-z_]+)`", line.split("The field is")[0]))
    assert names <= _members(Position), names - _members(Position)
