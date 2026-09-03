import pytest
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
