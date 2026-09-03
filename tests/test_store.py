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
