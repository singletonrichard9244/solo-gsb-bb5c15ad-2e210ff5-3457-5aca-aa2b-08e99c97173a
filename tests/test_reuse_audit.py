"""POST /events/commitments/reuse-audit 的跨事件承诺复用审计测试。

通过直接写库并显式指定 created_at，避免依赖墙钟；app.clock.now_utc
同样替换为固定时钟。覆盖：
  - 跨事件同哈希重复识别、所属事件 / 承诺 ID / 提交时间回显
  - exposed_before_commit 仅在「其他事件」同哈希揭示时间严格早于
    本项提交时间时为 true；同刻揭示、同事件揭示、事后揭示均不算
  - 多个揭示来源的过滤与稳定排序
  - 大小写归一（直接写入大写哈希也须与小写归并）
  - 请求内重复事件 ID 去重；缺失事件 404；非 UUID / 数量越界 422
  - 无重复时稳定空结果；结果按 (哈希, 提交时间, 承诺 ID) 稳定排序
  - 纯只读：审计不改变提交、揭示与结算
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_reuse_audit_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/reuse_audit.db"

from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Commitment, Event, Reveal, Settlement  # noqa: E402

BASE = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def t(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


class FixedClock:
    def __call__(self) -> datetime:
        return BASE


_client_clock = FixedClock()
client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(clock_module, "now_utc", _client_clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


HASH_A = "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"
HASH_B = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
HASH_C = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def make_event(event_id: UUID, title: str = "event") -> None:
    db = SessionLocal()
    try:
        db.add(
            Event(
                id=event_id,
                title=title,
                created_at=t(0),
                commit_deadline=t(100),
                reveal_deadline=t(200),
            )
        )
        db.commit()
    finally:
        db.close()


def add_commitment(
    event_id: UUID,
    commitment_hash: str,
    committed_at: datetime,
    commitment_id: UUID | None = None,
) -> UUID:
    db = SessionLocal()
    try:
        cid = commitment_id or uuid4()
        db.add(
            Commitment(
                id=cid,
                event_id=event_id,
                commitment_hash=commitment_hash,
                created_at=committed_at,
            )
        )
        db.commit()
        return cid
    finally:
        db.close()


def add_reveal(
    event_id: UUID,
    commitment_id: UUID,
    revealed_at: datetime,
    commitment_hash: str = HASH_A,
    reveal_id: UUID | None = None,
) -> UUID:
    db = SessionLocal()
    try:
        rid = reveal_id or uuid4()
        db.add(
            Reveal(
                id=rid,
                event_id=event_id,
                commitment_id=commitment_id,
                original_text="预测原文",
                salt="salt",
                recomputed_hash=commitment_hash,
                created_at=revealed_at,
            )
        )
        db.commit()
        return rid
    finally:
        db.close()


def audit(event_ids):
    return client.post(
        "/events/commitments/reuse-audit",
        json={"event_ids": [str(e) for e in event_ids]},
    )


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 请求体校验
# --------------------------------------------------------------------------- #

def test_too_few_event_ids_returns_422():
    a = uuid4()
    make_event(a)
    r = audit([a])
    assert r.status_code == 422
    r = client.post("/events/commitments/reuse-audit", json={"event_ids": []})
    assert r.status_code == 422
    r = client.post("/events/commitments/reuse-audit", json={})
    assert r.status_code == 422


def test_too_many_event_ids_returns_422():
    ids = [uuid4() for _ in range(51)]
    r = audit(ids)
    assert r.status_code == 422


def test_invalid_uuid_returns_422():
    a = uuid4()
    make_event(a)
    r = client.post(
        "/events/commitments/reuse-audit",
        json={"event_ids": [str(a), "not-a-uuid"]},
    )
    assert r.status_code == 422


def test_missing_event_returns_404():
    a = uuid4()
    make_event(a)
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        "/events/commitments/reuse-audit",
        json={"event_ids": [str(a), missing]},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "EVENT_NOT_FOUND"
    assert missing in detail["message"]


def test_duplicate_event_ids_deduplicated():
    # 去重后只剩 1 个唯一事件：不触发 min_length=2（去重在路由内完成），
    # 单事件无跨事件重复，稳定返回空结果
    a = uuid4()
    make_event(a)
    add_commitment(a, HASH_A, t(5))
    r = audit([a, a, a])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_count"] == 1
    assert body["items"] == []
    assert body["duplicate_hash_count"] == 0
    assert body["item_count"] == 0
    assert parse_iso(body["generated_at"]) == BASE


# --------------------------------------------------------------------------- #
# 重复识别与 exposed_before_commit 判定
# --------------------------------------------------------------------------- #

def test_basic_cross_event_duplicate_with_exposure():
    # B 先提交（t10）并在 t20 揭示；A 后提交（t30）：
    # A 的条目应被标记为「提交前已在其他事件公开」，来源为 B 的揭示
    a, b = uuid4(), uuid4()
    make_event(a, title="A")
    make_event(b, title="B")
    cid_b = add_commitment(b, HASH_A, t(10))
    rid_b = add_reveal(b, cid_b, t(20), commitment_hash=HASH_A)
    cid_a = add_commitment(a, HASH_A, t(30))

    r = audit([a, b])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_count"] == 2
    assert body["duplicate_hash_count"] == 1
    assert body["item_count"] == 2

    by_commitment = {item["commitment_id"]: item for item in body["items"]}

    item_a = by_commitment[str(cid_a)]
    assert item_a["event_id"] == str(a)
    assert item_a["commitment_hash"] == HASH_A
    assert parse_iso(item_a["committed_at"]) == t(30)
    assert item_a["exposed_before_commit"] is True
    assert item_a["exposure_sources"] == [
        {
            "event_id": str(b),
            "commitment_id": str(cid_b),
            "reveal_id": str(rid_b),
            "revealed_at": t(20).isoformat().replace("+00:00", "Z"),
        }
    ]

    # B 提交在前：没有任何更早的其他事件揭示
    item_b = by_commitment[str(cid_b)]
    assert item_b["event_id"] == str(b)
    assert parse_iso(item_b["committed_at"]) == t(10)
    assert item_b["exposed_before_commit"] is False
    assert item_b["exposure_sources"] == []


def test_same_instant_reveal_is_not_exposed():
    # 其他事件揭示时间恰等于本项提交时间：严格早于不成立，同刻不算公开
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    cid_b = add_commitment(b, HASH_A, t(10))
    add_reveal(b, cid_b, t(30), commitment_hash=HASH_A)
    cid_a = add_commitment(a, HASH_A, t(30))  # 与 B 的揭示同刻

    r = audit([a, b])
    body = r.json()
    by_commitment = {item["commitment_id"]: item for item in body["items"]}
    item_a = by_commitment[str(cid_a)]
    assert item_a["exposed_before_commit"] is False
    assert item_a["exposure_sources"] == []


def test_reveal_after_commit_is_not_exposed():
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    cid_a = add_commitment(a, HASH_A, t(10))
    cid_b = add_commitment(b, HASH_A, t(20))
    add_reveal(a, cid_a, t(30), commitment_hash=HASH_A)  # 事后揭示

    r = audit([a, b])
    body = r.json()
    for item in body["items"]:
        assert item["exposed_before_commit"] is False
        assert item["exposure_sources"] == []


def test_same_event_reveal_does_not_count_as_exposure():
    # 即使本事件同哈希承诺的揭示更早（绕过 API 直接写库构造），
    # 「其他事件」条件不满足，不算事先公开
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    # 同事件内两条同哈希（直接写库绕过唯一约束的常规路径不允许，
    # 因此这里用不同大小写规避 (event_id, hash) 唯一约束）
    cid_a1 = add_commitment(a, HASH_A, t(10))
    add_reveal(a, cid_a1, t(15), commitment_hash=HASH_A)
    cid_b = add_commitment(b, HASH_A, t(30))

    r = audit([a, b])
    body = r.json()
    by_commitment = {item["commitment_id"]: item for item in body["items"]}
    # B 的条目：A 的揭示属于其他事件且早于 t30，应标记
    item_b = by_commitment[str(cid_b)]
    assert item_b["exposed_before_commit"] is True
    assert {s["event_id"] for s in item_b["exposure_sources"]} == {str(a)}
    # A 自己的条目：同事件揭示不算
    item_a = by_commitment[str(cid_a1)]
    assert item_a["exposed_before_commit"] is False
    assert item_a["exposure_sources"] == []


def test_case_insensitive_hash_grouping():
    # 存储中混有大写哈希：审计按大小写归一并视为同一重复项
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    cid_b = add_commitment(b, HASH_A.upper(), t(10))
    add_reveal(b, cid_b, t(20), commitment_hash=HASH_A.upper())
    cid_a = add_commitment(a, HASH_A, t(30))

    r = audit([a, b])
    body = r.json()
    assert body["duplicate_hash_count"] == 1
    assert body["item_count"] == 2
    assert {item["commitment_hash"] for item in body["items"]} == {HASH_A}
    by_commitment = {item["commitment_id"]: item for item in body["items"]}
    assert by_commitment[str(cid_a)]["exposed_before_commit"] is True
    # 大写来源的揭示同样应被识别
    assert by_commitment[str(cid_a)]["exposure_sources"][0]["event_id"] == str(b)


def test_non_duplicate_hash_excluded():
    # HASH_B 仅出现在一个事件中：不应出现在审计结果
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    add_commitment(a, HASH_A, t(5))
    add_commitment(b, HASH_A, t(6))
    add_commitment(b, HASH_B, t(7))

    r = audit([a, b])
    body = r.json()
    assert body["duplicate_hash_count"] == 1
    assert body["item_count"] == 2
    assert {item["commitment_hash"] for item in body["items"]} == {HASH_A}


def test_events_without_any_duplicates_return_empty():
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    add_commitment(a, HASH_A, t(1))
    add_commitment(b, HASH_B, t(1))

    r = audit([a, b])
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["duplicate_hash_count"] == 0
    assert body["item_count"] == 0
    assert body["event_count"] == 2


def test_multiple_exposure_sources_filtered_and_sorted():
    # 三个其他事件在不同时刻揭示同哈希，另有同刻 / 更晚的揭示：
    # 仅严格早于本项提交时间的来源被列出，且按 revealed_at 排序
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    for e in (a, b, c, d):
        make_event(e)
    cid_b = add_commitment(b, HASH_A, t(5))
    rid_b = add_reveal(b, cid_b, t(20), commitment_hash=HASH_A)
    cid_c = add_commitment(c, HASH_A, t(5))
    rid_c = add_reveal(c, cid_c, t(10), commitment_hash=HASH_A)
    cid_d = add_commitment(d, HASH_A, t(5))
    add_reveal(d, cid_d, t(40), commitment_hash=HASH_A)  # 晚于 A 的提交，排除
    cid_a = add_commitment(a, HASH_A, t(30))
    # 另一事件在同刻揭示：t30 == 提交时间，严格早于不成立，排除
    e = uuid4()
    make_event(e)
    cid_e = add_commitment(e, HASH_A, t(5))
    add_reveal(e, cid_e, t(30), commitment_hash=HASH_A)  # 同刻，排除

    r = audit([a, b, c, d, e])
    body = r.json()
    by_commitment = {item["commitment_id"]: item for item in body["items"]}
    item_a = by_commitment[str(cid_a)]
    assert item_a["exposed_before_commit"] is True
    sources = item_a["exposure_sources"]
    assert [s["reveal_id"] for s in sources] == [str(rid_c), str(rid_b)]
    assert [parse_iso(s["revealed_at"]) for s in sources] == [t(10), t(20)]
    assert {s["event_id"] for s in sources} == {str(c), str(b)}


# --------------------------------------------------------------------------- #
# 排序
# --------------------------------------------------------------------------- #

def test_items_sorted_by_hash_then_committed_at_then_id():
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    # 两个重复哈希，各自跨 A/B；故意打乱提交时间顺序
    id_small = UUID("00000000-0000-0000-0000-000000000001")
    id_large = UUID("00000000-0000-0000-0000-000000000002")
    # HASH_A 同刻两条：靠 commitment_id 决定先后
    add_commitment(a, HASH_B, t(1))
    add_commitment(b, HASH_B, t(2))
    add_commitment(a, HASH_A, t(5), commitment_id=id_large)
    add_commitment(b, HASH_A, t(5), commitment_id=id_small)

    r = audit([a, b])
    body = r.json()
    assert body["item_count"] == 4
    items = body["items"]

    keys = [
        (
            item["commitment_hash"],
            parse_iso(item["committed_at"]),
            UUID(item["commitment_id"]).int,
        )
        for item in items
    ]
    assert keys == sorted(keys)
    #具体顺序：HASH_A(t5, small) -> HASH_A(t5, large) -> HASH_B(t1) -> HASH_B(t2)
    assert items[0]["commitment_hash"] == HASH_A
    assert items[0]["commitment_id"] == str(id_small)
    assert items[1]["commitment_id"] == str(id_large)
    assert items[2]["commitment_hash"] == HASH_B
    assert parse_iso(items[2]["committed_at"]) == t(1)
    assert parse_iso(items[3]["committed_at"]) == t(2)


def test_exposure_sources_tie_break_sorting():
    # 两个来源揭示时间相同：按 event_id 再按 reveal_id 排序
    a = uuid4()
    src1 = UUID("11111111-1111-1111-1111-111111111111")
    src2 = UUID("22222222-2222-2222-2222-222222222222")
    make_event(a)
    make_event(src1)
    make_event(src2)
    cid1 = add_commitment(src1, HASH_A, t(1))
    cid2 = add_commitment(src2, HASH_A, t(1))
    add_reveal(src1, cid1, t(10), commitment_hash=HASH_A)
    add_reveal(src2, cid2, t(10), commitment_hash=HASH_A)
    cid_a = add_commitment(a, HASH_A, t(20))

    r = audit([a, src1, src2])
    body = r.json()
    by_commitment = {item["commitment_id"]: item for item in body["items"]}
    source_events = [s["event_id"] for s in by_commitment[str(cid_a)]["exposure_sources"]]
    assert source_events == [str(src1), str(src2)]


# --------------------------------------------------------------------------- #
# 只读性与确定性
# --------------------------------------------------------------------------- #

def test_audit_is_readonly_and_deterministic():
    a, b = uuid4(), uuid4()
    make_event(a)
    make_event(b)
    cid_b = add_commitment(b, HASH_A, t(10))
    add_reveal(b, cid_b, t(20), commitment_hash=HASH_A)
    add_commitment(a, HASH_A, t(30))

    db = SessionLocal()
    try:
        before_commitments = db.query(Commitment).count()
        before_reveals = db.query(Reveal).count()
        before_settlements = db.query(Settlement).count()
    finally:
        db.close()

    r1 = audit([b, a])  # 乱序
    r2 = audit([a, b, a])  # 含重复
    assert r1.status_code == 200
    assert r1.json() == r2.json()  # 等价输入逐字节一致

    db = SessionLocal()
    try:
        assert db.query(Commitment).count() == before_commitments
        assert db.query(Reveal).count() == before_reveals
        assert db.query(Settlement).count() == before_settlements
    finally:
        db.close()


def test_unrevealed_duplicates_have_empty_sources():
    a, b, c = uuid4(), uuid4(), uuid4()
    for e in (a, b, c):
        make_event(e)
    add_commitment(a, HASH_C, t(1))
    add_commitment(b, HASH_C, t(2))
    add_commitment(c, HASH_C, t(3))

    r = audit([a, b, c])
    body = r.json()
    assert body["duplicate_hash_count"] == 1
    assert body["item_count"] == 3
    for item in body["items"]:
        assert item["exposed_before_commit"] is False
        assert item["exposure_sources"] == []
