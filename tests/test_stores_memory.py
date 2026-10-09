"""MemoryStore: the dict-backed ObjectStore the sink tests drive."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from evalshift.stores import MemoryStore, ObjectStore


def test_satisfies_protocol() -> None:
    assert isinstance(MemoryStore(), ObjectStore)


def test_put_then_objects_snapshot() -> None:
    store = MemoryStore()
    store.put("captures/s/cap_a.json", b"{}")
    store.put("toolsets/ab.json", b"[]")
    assert store.objects == {"captures/s/cap_a.json": b"{}", "toolsets/ab.json": b"[]"}
    snapshot = store.objects
    store.put("captures/s/cap_b.json", b"{}")
    assert "captures/s/cap_b.json" not in snapshot  # objects is a copy, not a live view


def test_put_overwrites_same_key() -> None:
    store = MemoryStore()
    store.put("k", b"1")
    store.put("k", b"2")
    assert store.objects == {"k": b"2"}


def test_concurrent_puts_are_all_kept() -> None:
    store = MemoryStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: store.put(f"k{i}", b"x"), range(200)))
    assert len(store.objects) == 200


def test_uri_is_memory() -> None:
    assert MemoryStore().uri == "memory://"
