"""Nursery feature: organisms live in organisms/<name>/ subdirectories,
a `current` pointer file remembers who is awake, /new births from the
seed genome, and a legacy root-level organism migrates into
organisms/default/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import nursery

SEED_SCL = 'type bel(x: String, a: String, v: String)\n'


def _root(tmp_path):
    (tmp_path / "organism.scl").write_text(SEED_SCL)
    return tmp_path


def test_create_births_from_template(tmp_path):
    root = _root(tmp_path)
    dest = nursery.create(root, "fern", root / "organism.scl")
    assert dest == root / "organisms" / "fern"
    assert (dest / "organism.scl").read_text() == SEED_SCL
    assert nursery.list_organisms(root) == ["fern"]


def test_create_rejects_duplicate_and_invalid_names(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    with pytest.raises(ValueError, match="already exists"):
        nursery.create(root, "fern", root / "organism.scl")
    for bad in ("has space", "a/b", "", "dots.name", "emoji\u2603"):
        with pytest.raises(ValueError, match="invalid organism name"):
            nursery.create(root, bad, root / "organism.scl")


def test_create_allows_capital_letters(tmp_path):
    root = _root(tmp_path)
    dest = nursery.create(root, "Fern", root / "organism.scl")
    assert dest == root / "organisms" / "Fern"
    nursery.create(root, "Replicanta-2_X", root / "organism.scl")
    assert nursery.list_organisms(root) == ["Fern", "Replicanta-2_X"]


def test_list_organisms_empty_and_sorted(tmp_path):
    root = _root(tmp_path)
    assert nursery.list_organisms(root) == []
    for name in ("zoe", "amy", "fern"):
        nursery.create(root, name, root / "organism.scl")
    assert nursery.list_organisms(root) == ["amy", "fern", "zoe"]


def test_next_name_skips_taken(tmp_path):
    root = _root(tmp_path)
    assert nursery.next_name(root) == "replicanta-2"
    nursery.create(root, "replicanta-2", root / "organism.scl")
    assert nursery.next_name(root) == "replicanta-3"


def test_current_defaults_and_persists(tmp_path):
    root = _root(tmp_path)
    assert nursery.current(root) == "default"
    nursery.set_current(root, "fern")
    assert nursery.current(root) == "fern"


def test_migrate_moves_legacy_root_organism(tmp_path):
    root = _root(tmp_path)
    (root / "state.json").write_text('{"cycle": 42}')
    (root / "artifacts").mkdir()
    (root / "artifacts" / "diary.md").write_text("dear diary")
    assert nursery.migrate(root) is True
    dest = root / "organisms" / "default"
    assert (dest / "state.json").read_text() == '{"cycle": 42}'
    assert (dest / "artifacts" / "diary.md").read_text() == "dear diary"
    assert (dest / "organism.scl").read_text() == SEED_SCL
    assert not (root / "state.json").exists()      # moved, not copied
    assert not (root / "artifacts").exists()
    assert (root / "organism.scl").exists()        # template stays
    assert nursery.current(root) == "default"


def test_migrate_noop_without_state_or_when_default_exists(tmp_path):
    root = _root(tmp_path)
    assert nursery.migrate(root) is False          # nothing to migrate
    (root / "state.json").write_text("{}")
    nursery.create(root, "default", root / "organism.scl")
    assert nursery.migrate(root) is False          # default already there
    assert (root / "state.json").exists()          # untouched


# -- rename -----------------------------------------------------------------

def test_rename_moves_directory(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    dest = nursery.rename(root, "fern", "willow")
    assert dest == root / "organisms" / "willow"
    assert (dest / "organism.scl").read_text() == SEED_SCL
    assert nursery.list_organisms(root) == ["willow"]


def test_rename_repoints_current_pointer(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    nursery.set_current(root, "fern")
    nursery.rename(root, "fern", "willow")
    assert nursery.current(root) == "willow"


def test_rename_leaves_current_pointer_alone_for_sleepers(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    nursery.create(root, "moss", root / "organism.scl")
    nursery.set_current(root, "moss")
    nursery.rename(root, "fern", "willow")
    assert nursery.current(root) == "moss"


def test_rename_rejects_bad_names_and_missing_or_taken(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    nursery.create(root, "moss", root / "organism.scl")
    with pytest.raises(ValueError, match="invalid organism name"):
        nursery.rename(root, "fern", "has space")
    with pytest.raises(ValueError, match="no organism named"):
        nursery.rename(root, "ghost", "willow")
    with pytest.raises(ValueError, match="already exists"):
        nursery.rename(root, "fern", "moss")
    # failed renames leave the nursery untouched
    assert nursery.list_organisms(root) == ["fern", "moss"]


def test_rename_to_capitalized_name(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    dest = nursery.rename(root, "fern", "Fern")
    assert dest == root / "organisms" / "Fern"
    assert (dest / "organism.scl").read_text() == SEED_SCL
    assert nursery.list_organisms(root) == ["Fern"]


def test_rename_case_change_repoints_current(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    nursery.set_current(root, "fern")
    nursery.rename(root, "fern", "FERN")
    assert nursery.current(root) == "FERN"


def test_rename_same_name_is_a_noop(tmp_path):
    root = _root(tmp_path)
    nursery.create(root, "fern", root / "organism.scl")
    assert nursery.rename(root, "fern", "fern") == root / "organisms" / "fern"
    assert nursery.list_organisms(root) == ["fern"]


# -- nursery groups -----------------------------------------------------------

def _root_with(root, *names):
    for name in names:
        nursery.create(root, name, root / "organism.scl")
    return root


def test_create_and_list_groups(tmp_path):
    root = _root(tmp_path)
    nursery.create_group(root, "thinkers")
    nursery.create_group(root, "busy bees")
    assert nursery.list_groups(root) == ["busy bees", "thinkers"]


def test_create_group_rejects_invalid_and_duplicate(tmp_path):
    root = _root(tmp_path)
    nursery.create_group(root, "thinkers")
    with pytest.raises(ValueError, match="already exists"):
        nursery.create_group(root, "thinkers")
    for bad in ("a/b", "", " leading-space", "emoji☃", "x" * 40):
        with pytest.raises(ValueError, match="invalid group name"):
            nursery.create_group(root, bad)


def test_group_names_may_have_custom_characters(tmp_path):
    root = _root(tmp_path)
    nursery.create_group(root, "Night Shift 2.0_beta-1")
    assert nursery.list_groups(root) == ["Night Shift 2.0_beta-1"]


def test_assign_moves_organism_between_groups(tmp_path):
    root = _root_with(_root(tmp_path), "fern", "willow")
    nursery.create_group(root, "a")
    nursery.create_group(root, "b")
    nursery.assign(root, "fern", "a")
    assert nursery.group_of(root, "fern") == "a"
    nursery.assign(root, "fern", "b")
    assert nursery.group_of(root, "fern") == "b"
    assert nursery.load_groups(root) == {"a": [], "b": ["fern"]}
    nursery.assign(root, "fern", None)
    assert nursery.group_of(root, "fern") is None
    assert "fern" not in nursery.load_groups(root)["b"]


def test_assign_rejects_unknown_organism_and_group(tmp_path):
    root = _root_with(_root(tmp_path), "fern")
    nursery.create_group(root, "a")
    with pytest.raises(ValueError, match="no organism"):
        nursery.assign(root, "ghost", "a")
    with pytest.raises(ValueError, match="no group"):
        nursery.assign(root, "fern", "nowhere")


def test_rename_group_keeps_members(tmp_path):
    root = _root_with(_root(tmp_path), "fern")
    nursery.create_group(root, "old")
    nursery.assign(root, "fern", "old")
    nursery.rename_group(root, "old", "new")
    assert nursery.load_groups(root) == {"new": ["fern"]}
    assert nursery.group_of(root, "fern") == "new"
    with pytest.raises(ValueError, match="no group"):
        nursery.rename_group(root, "missing", "x")
    nursery.create_group(root, "taken")
    with pytest.raises(ValueError, match="already exists"):
        nursery.rename_group(root, "new", "taken")


def test_remove_group_ungroups_members(tmp_path):
    root = _root_with(_root(tmp_path), "fern")
    nursery.create_group(root, "a")
    nursery.assign(root, "fern", "a")
    nursery.remove_group(root, "a")
    assert nursery.list_groups(root) == []
    assert nursery.group_of(root, "fern") is None
    with pytest.raises(ValueError, match="no group"):
        nursery.remove_group(root, "a")


def test_load_groups_prunes_deleted_organisms(tmp_path):
    root = _root_with(_root(tmp_path), "fern", "willow")
    nursery.create_group(root, "a")
    nursery.assign(root, "fern", "a")
    nursery.assign(root, "willow", "a")
    import shutil
    shutil.rmtree(root / "organisms" / "willow")
    assert nursery.load_groups(root) == {"a": ["fern"]}


def test_load_groups_tolerates_missing_and_corrupt_file(tmp_path):
    root = _root(tmp_path)
    assert nursery.load_groups(root) == {}
    (root / "groups.json").write_text("{not json")
    assert nursery.load_groups(root) == {}
    (root / "groups.json").write_text('["not", "a", "dict"]')
    assert nursery.load_groups(root) == {}


def test_groups_persist_across_loads(tmp_path):
    root = _root_with(_root(tmp_path), "fern")
    nursery.create_group(root, "a")
    nursery.assign(root, "fern", "a")
    # a fresh read sees the same state (file-backed, not in-memory)
    assert nursery.load_groups(root) == {"a": ["fern"]}
    assert (root / "groups.json").exists()


def test_empty_groups_survive_loads(tmp_path):
    root = _root(tmp_path)
    nursery.create_group(root, "vacant")
    assert nursery.load_groups(root) == {"vacant": []}
