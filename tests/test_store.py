import pytest
import subprocess
from pathlib import Path
from court.models import Quest
from court import store


def test_store_lifecycle(tmp_path):
    court_root = tmp_path / ".court"

    # 1. next_number starts at 1
    assert store.next_number(court_root) == 1

    # 2. make_id
    qid = store.make_id("api", "rate-limit", court_root=court_root)
    assert qid == "Q001-Api-Rate-Limit"

    # 3. save quest
    q = Quest(id=qid, title="Rate Limiting", app="api", concern="rate-limit")
    p = store.save(q, court_root=court_root)
    assert p.exists()
    assert store.next_number(court_root) == 2

    # 4. load quest
    loaded = store.load(qid, court_root=court_root)
    assert loaded.id == qid
    assert loaded.title == "Rate Limiting"

    # 5. load by number / prefix
    loaded_by_num = store.load("1", court_root=court_root)
    assert loaded_by_num.id == qid

    # 6. list_all
    all_quests = store.list_all(court_root=court_root)
    assert len(all_quests) == 1
    assert all_quests[0].id == qid

    # 7. archive
    archived_path = store.archive(qid, court_root=court_root)
    assert archived_path.exists()
    assert not p.exists()
    assert len(store.list_all(include_archive=False, court_root=court_root)) == 0
    assert len(store.list_all(include_archive=True, court_root=court_root)) == 1
    # next_number preserves monotonic counter across archived items
    assert store.next_number(court_root) == 2


def test_store_hierarchy(tmp_path):
    court_root = tmp_path / ".court"

    epic = Quest(id="Q010-Auth-Migration", title="Auth Migration", kind="epic", app="auth", concern="migration")
    child1 = Quest(id="Q011-Auth-Jwt", title="JWT Support", kind="quest", app="auth", concern="jwt", parent_epic="Q010-Auth-Migration")
    child2 = Quest(id="Q012-Auth-OAuth", title="OAuth Support", kind="quest", app="auth", concern="oauth", parent_epic="Q010")
    standalone = Quest(id="Q013-Billing-Stripe", title="Stripe Billing", kind="quest", app="billing", concern="stripe", section="Feature")
    scout = Quest(id="Q014-Ai-Search", title="AI Search Spike", kind="scout", app="search", concern="ai")

    for item in [epic, child1, child2, standalone, scout]:
        store.save(item, court_root=court_root)

    hierarchy = store.get_hierarchy(court_root=court_root)
    assert len(hierarchy["epics"]) == 1
    ep, children = hierarchy["epics"][0]
    assert ep.id == "Q010-Auth-Migration"
    assert len(children) == 2
    assert {c.id for c in children} == {"Q011-Auth-Jwt", "Q012-Auth-OAuth"}

    assert len(hierarchy["standalone"]) == 1
    assert hierarchy["standalone"][0].id == "Q013-Billing-Stripe"

    assert len(hierarchy["scouts"]) == 1
    assert hierarchy["scouts"][0].id == "Q014-Ai-Search"


def test_rollup_ship_manifest(tmp_path):
    court_root = tmp_path / ".court"

    tribute_body = """### 1. Ballad
Shipped the auth refactor cleanly.

### 2. Tribute
- Added 2 tests, all passing.

### 3. Penance
Deferred Redis failover edge case.

### 4. Audience
None required.

### 5. Humble Opinion
Recommend a follow-up cleanup Quest.
"""
    done_quest = Quest(
        id="Q020-Auth-Cleanup",
        title="Auth Cleanup",
        kind="quest",
        app="auth",
        concern="cleanup",
        status="READY_FOR_TEARDOWN",
    )
    done_quest.set_section("Tribute Rendered", tribute_body)

    open_quest = Quest(
        id="Q021-Auth-Other",
        title="Auth Other Work",
        kind="quest",
        app="auth",
        concern="other",
        status="WORKING",
    )

    store.save(done_quest, court_root=court_root)
    store.save(open_quest, court_root=court_root)

    manifest = store.rollup_ship_manifest(court_root=court_root)
    assert len(manifest["quests"]) == 1
    assert manifest["quests"][0].id == "Q020-Auth-Cleanup"
    assert len(manifest["ballads"]) == 1
    assert "Shipped the auth refactor cleanly" in manifest["ballads"][0][1]
    assert len(manifest["tributes"]) == 1
    assert len(manifest["penances"]) == 1
    assert len(manifest["opinions"]) == 1

    # WORKING quest excluded by default; included when status filter widened
    manifest_all = store.rollup_ship_manifest(status="WORKING,READY_FOR_TEARDOWN", court_root=court_root)
    assert len(manifest_all["quests"]) == 2


def _init_git_repo(repo_dir: Path, branch: str = "castle") -> None:
    """Minimal git repo with one commit on `branch`, for auto-commit tests."""
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "initial"], check=True)


def test_save_default_matches_plain_write_byte_for_byte(tmp_path):
    """Default `save()` (auto_commit=False) must remain identical to the
    historical plain-write behavior for every existing caller."""
    court_root = tmp_path / ".court"
    q = Quest(id="Q001-Test-Plain", title="Plain Save", app="test", concern="plain")

    p1 = store.save(q, court_root=court_root)
    content_a = p1.read_text(encoding="utf-8")

    # Calling again with only the new kwargs at their defaults must be a no-op
    # difference from a caller's perspective.
    p2 = store.save(q, court_root=court_root, auto_commit=False, commit_msg=None)
    content_b = p2.read_text(encoding="utf-8")

    assert p1 == p2
    assert content_a == content_b


def test_commit_allowed_here_guard_blocks_wrong_branch(tmp_path):
    """`auto_commit=True` must refuse to write when the current checkout is
    neither a protected trunk nor the Quest's own branch."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir, branch="some-unrelated-branch")

    court_root = repo_dir / ".court"
    q = Quest(
        id="Q002-Test-Guard",
        title="Guarded Save",
        app="test",
        concern="guard",
        branch="quest/q002-test-guard",
    )

    p = store.save(q, court_root=court_root, auto_commit=True, commit_msg="test: guard")
    assert not p.exists()  # refused before writing to disk


def test_commit_allowed_here_guard_allows_protected_trunk(tmp_path):
    """`auto_commit=True` succeeds (and actually commits) from a protected
    trunk branch (castle/main/the-gatehouse/*)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir, branch="castle")

    court_root = repo_dir / ".court"
    q = Quest(id="Q003-Test-Trunk", title="Trunk Save", app="test", concern="trunk")

    p = store.save(q, court_root=court_root, auto_commit=True, commit_msg="test: trunk save")
    assert p.exists()

    log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    )
    assert "test: trunk save" in log.stdout


def test_commit_allowed_here_guard_allows_own_branch(tmp_path):
    """`auto_commit=True` succeeds when the current checkout matches the
    Quest's own registered branch (a Serf editing its own Quest)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir, branch="quest/q004-test-own-branch")

    court_root = repo_dir / ".court"
    q = Quest(
        id="Q004-Test-Own",
        title="Own Branch Save",
        app="test",
        concern="own",
        branch="quest/q004-test-own-branch",
    )

    p = store.save(q, court_root=court_root, auto_commit=True, commit_msg="test: own branch save")
    assert p.exists()


def test_stamp_cogship_allocates_and_persists(tmp_path):
    court_root = tmp_path / ".court"
    q1 = Quest(id="Q010-Test-Cog1", title="Cog Quest 1", app="test", concern="cog1")
    q2 = Quest(id="Q011-Test-Cog2", title="Cog Quest 2", app="test", concern="cog2")
    store.save(q1, court_root=court_root)
    store.save(q2, court_root=court_root)

    stamped_id = store.stamp_cogship([q1, q2], court_root=court_root)
    assert stamped_id == "cogship-001"
    assert q1.cogship_id == "cogship-001"
    assert q2.cogship_id == "cogship-001"

    reloaded = store.load("Q010-Test-Cog1", court_root=court_root)
    assert reloaded.cogship_id == "cogship-001"

    # A second batch allocates the next monotonic id.
    q3 = Quest(id="Q012-Test-Cog3", title="Cog Quest 3", app="test", concern="cog3")
    store.save(q3, court_root=court_root)
    stamped_id_2 = store.stamp_cogship([q3], court_root=court_root)
    assert stamped_id_2 == "cogship-002"


def test_normalize_cogship_id():
    assert store.normalize_cogship_id("2") == "cogship-002"
    assert store.normalize_cogship_id("cogship-7") == "cogship-007"
    assert store.normalize_cogship_id("cogship_012") == "cogship-012"
    assert store.normalize_cogship_id("") is None
    assert store.normalize_cogship_id(None) is None
