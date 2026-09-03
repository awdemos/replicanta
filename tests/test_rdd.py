"""Tests for the lazy RDD subsystem and visual renderer."""

from replicanta import rdd


class FakeStore:
    def __init__(self, beliefs=None, activity=None, memory=None, mood_history=None, stress_history=None):
        self.beliefs_map = beliefs or {}
        self.activity = activity or {}
        self.memory = memory or []
        self.mood_history = mood_history or []
        self.stress_history = stress_history or []

    def beliefs(self):
        return dict(self.beliefs_map)


def test_dataset_lazy_execution():
    ds = rdd.Dataset.from_records([{"x": 1}, {"x": 2}, {"x": 3}])
    mapped = ds.map(lambda r: {"x": r["x"] * 2})
    filtered = mapped.filter(lambda r: r["x"] > 2)
    assert filtered.collect() == [{"x": 4}, {"x": 6}]
    assert filtered.count() == 2


def test_group_by_and_sort():
    ds = rdd.Dataset.from_records([{"color": "red"}, {"color": "blue"}, {"color": "red"}])
    grouped = ds.group_by(lambda r: r["color"]).sort(lambda r: -r["count"])
    result = grouped.collect()
    assert result == [
        {"key": "red", "count": 2, "values": [{"color": "red"}, {"color": "red"}]},
        {"key": "blue", "count": 1, "values": [{"color": "blue"}]},
    ]


def test_lineage_nodes_and_edges():
    ds = (
        rdd.Dataset.from_records([{"x": 1}, {"x": 2}])
        .map(lambda r: {"x": r["x"] + 1}, label="inc")
        .filter(lambda r: r["x"] > 1, label="gt1")
    )
    nodes, edges = ds.lineage()
    assert len(nodes) == 3
    assert len(edges) == 2
    assert edges[0][2] == "map"
    assert edges[1][2] == "filter"


def test_render_lineage_svg():
    ds = rdd.Dataset.from_records([{"x": 1}]).map(lambda r: {"x": r["x"]})
    svg = rdd.render_lineage_svg(ds, title="test lineage")
    assert "<svg" in svg
    assert "test lineage" in svg
    assert "<circle" in svg


def test_render_bar_chart_svg():
    records = [{"key": "a", "count": 10}, {"key": "b", "count": 5}]
    svg = rdd.render_bar_chart_svg(records, "count", "key", title="test chart")
    assert "<svg" in svg
    assert "test chart" in svg
    assert "a" in svg
    assert "10" in svg


def test_render_text_bars():
    records = [{"key": "a", "count": 10}, {"key": "b", "count": 5}]
    text = rdd.render_text_bars(records, "count", "key", width=10)
    assert "a" in text
    assert "b" in text
    assert "█" in text


def test_fmt_number():
    assert rdd._fmt_number(1234) == "1.2K"
    assert rdd._fmt_number(1_500_000) == "1.5M"
    assert rdd._fmt_number(42) == "42"


def test_belief_by_object_dataset_uses_confidence():
    store = FakeStore(
        beliefs={
            ("user", "name", "sam"): 0.9,
            ("user", "likes", "moss"): 0.8,
            ("self", "mood", "calm"): 0.7,
        }
    )
    ds = rdd.belief_by_object_dataset(store)
    result = ds.collect()
    counts = {r["key"]: r["count"] for r in result}
    assert counts["user"] == 1.7
    assert counts["self"] == 0.7


def test_belief_attribute_dataset():
    store = FakeStore(beliefs={("user", "name", "sam"): 0.9, ("self", "mood", "calm"): 0.8})
    ds = rdd.belief_attribute_dataset(store)
    result = ds.collect()
    keys = {r["key"] for r in result}
    assert "user.name" in keys
    assert "self.mood" in keys


def test_activity_dataset():
    store = FakeStore(activity={"llm_calls": 5, "derivations": 3, "snapshots": []})
    ds = rdd.activity_dataset(store)
    result = ds.collect()
    keys = {r["key"] for r in result}
    assert "llm_calls" in keys
    assert "derivations" in keys
    assert "snapshots" not in keys


def test_memory_kind_dataset():
    store = FakeStore(memory=[{"kind": "learned"}, {"kind": "learned"}, {"kind": "dream"}])
    ds = rdd.memory_kind_dataset(store)
    result = ds.collect()
    counts = {r["key"]: r["count"] for r in result}
    assert counts == {"learned": 2, "dream": 1}


def test_recent_memories_dataset():
    store = FakeStore(memory=[{"kind": "born", "cycle": 1}, {"kind": "learned", "cycle": 3}])
    ds = rdd.recent_memories_dataset(store)
    result = ds.collect()
    assert result[0]["key"].startswith("learned")


def test_mood_dataset():
    store = FakeStore(mood_history=[{"mood": "curious"}, {"mood": "curious"}, {"mood": "calm"}])
    ds = rdd.mood_dataset(store)
    result = ds.collect()
    counts = {r["key"]: r["count"] for r in result}
    assert counts == {"curious": 2, "calm": 1}


def test_summary_picks_richest():
    store_with_memories = FakeStore(memory=[{"kind": "x"}] * 10)
    store_with_beliefs = FakeStore(
        beliefs={
            ("a", "b", "c"): 0.5,
            ("a", "b2", "c2"): 0.5,
            ("a2", "b", "c"): 0.5,
            ("a3", "b", "c"): 0.5,
            ("a4", "b", "c"): 0.5,
        }
    )
    assert rdd._best_summary_kind(store_with_memories) == "memories"
    assert rdd._best_summary_kind(store_with_beliefs) == "beliefs"


def test_build_chart_summary():
    store = FakeStore(beliefs={("a", "b", "c"): 0.5, ("a", "d", "e"): 0.6})
    result = rdd.build_chart("summary", store)
    assert result["kind"] in rdd.PRESETS
    assert result["text_chart"]
    assert "<svg" in result["chart_svg"]


def test_supported_kinds():
    kinds = rdd.supported_kinds()
    assert "summary" in kinds
    assert "beliefs" in kinds
    assert "mood" in kinds
    assert "attributes" in kinds
