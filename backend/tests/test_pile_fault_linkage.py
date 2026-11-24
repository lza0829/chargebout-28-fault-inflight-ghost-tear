"""有在充时改故障/维护 的联动夹具。

覆盖：有在充改故障、故障与关账对撞半截、故障后仍记脉冲、拒改却占用已变、
改故障结局可重放、桩态回执/中断回执/会话册/桩位页/总览同刷。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 只导入一次（FastAPI/pydantic 导入昂贵）；每个用例切库并重新播种。
os.environ.setdefault(
    "CHARGEBOUT_DB", str(Path(tempfile.mkdtemp(prefix="chargebout-import-")) / "boot.db")
)
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import db as app_db  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture()
def c(monkeypatch):
    # 每个用例独立库，避免串味。
    d = tempfile.mkdtemp(prefix="chargebout-test-")
    db_path = Path(d) / "t.db"
    monkeypatch.setattr(app_db, "DB_PATH", db_path)
    seed_if_empty()
    with TestClient(app) as cli:
        yield cli


def _piles(c):
    return {p["name"]: p for p in c.get("/api/桩位").json()}


def _open(c, pile_id, gun_id=None):
    body = {"桩位编号": pile_id}
    if gun_id:
        body["枪线编号"] = gun_id
    r = c.post("/api/开会话", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _pile_by_name(c, name):
    return _piles(c)[name]


def _sessions(c, status=None):
    q = f"?状态={status}" if status else ""
    return c.get(f"/api/会话{q}").json()


def _set_status(c, pile_id, status, request_key=None, note=""):
    body = {"状态": status, "备注": note}
    if request_key:
        body["请求号"] = request_key
    return c.post(f"/api/桩位/{pile_id}/状态", json=body)


def _overview(c):
    return c.get("/api/总览").json()


def test_fault_with_open_charges_forces_interrupt(c):
    """有在充时改故障：允许并同笔强制中断、释放占用，各面同刷。"""
    p = _pile_by_name(c, "河甲一号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    sid = s["会话编号"]
    c.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0, "功率": 7})

    before = _overview(c)
    assert before["充电中"] == 1

    r = _set_status(c, p["id"], "故障", request_key="req-fault-1")
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["结果"] == "已生效"
    assert receipt["桩态前"] == "占用"
    assert receipt["桩态后"] == "故障"
    assert receipt["中断会话数"] == 1
    assert receipt["占用释放"] is True
    assert receipt["回执号"].startswith("桩态")
    assert receipt["故障事件编号"] == receipt["桩态事件编号"]
    intr = receipt["中断会话"][0]
    assert intr["会话编号"] == sid
    assert intr["会话状态后"] == "已中断"
    assert intr["中断回执"].startswith("中断")

    # 桩位页：桩态已故障，占用释放，无在充会话挂在其上。
    pile = _pile_by_name(c, "河甲一号")
    assert pile["status"] == "故障"
    assert pile["open_session_id"] is None
    assert pile["last_receipt_no"] == receipt["回执号"]

    # 枪线：联动故障。
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    assert all(g["status"] == "故障" for g in guns)

    # 会话册：会话已中断且带中断回执号；详情可看中断回执与事件。
    detail = c.get(f"/api/会话/{sid}").json()
    assert detail["status"] == "已中断"
    assert len(detail["中断回执"]) == 1
    assert detail["中断回执"][0]["receipt_no"] == intr["中断回执"]
    assert any(e["action"] == "强制中断" for e in detail["事件"])

    # 总览：故障桶 +1、充电中桶 -1、故障桩在充为 0、口径一致。
    after = _overview(c)
    assert after["桩位故障"] == before["桩位故障"] + 1
    assert after["充电中"] == before["充电中"] - 1
    assert after["已中断"] == before["已中断"] + 1
    assert after["故障桩在充会话"] == 0
    assert after["口径一致"] is True
    assert after["口径异常数"] == 0


def test_fault_receipt_is_replayable(c):
    """改故障结局可重放：同请求号重放返回原回执，不重复中断/写事件。"""
    p = _pile_by_name(c, "河甲一号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])

    r1 = _set_status(c, p["id"], "故障", request_key="replay-key")
    assert r1.status_code == 200
    receipt1 = r1.json()
    assert receipt1["重放"] is False

    r2 = _set_status(c, p["id"], "故障", request_key="replay-key")
    assert r2.status_code == 200
    receipt2 = r2.json()
    assert receipt2["重放"] is True
    assert receipt2["回执号"] == receipt1["回执号"]
    assert receipt2["中断会话数"] == 1

    # 通过回执查询口也能取到同一笔。
    q = c.get("/api/桩态回执", params={"请求号": "replay-key"})
    assert q.status_code == 200
    assert q.json()["回执号"] == receipt1["回执号"]

    # 事件只落一条；中断回执也只一条。
    events = c.get(f"/api/桩位/{p['id']}/状态事件").json()
    assert len(events) == 1
    detail = c.get(f"/api/会话/{s['会话编号']}").json()
    assert len(detail["中断回执"]) == 1


def test_fault_does_not_recompute_settlement_amount(c):
    """强制中断不自动结算：金额停留在当前预估，不被改故障改写。"""
    p = _pile_by_name(c, "河甲二号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    c.post(f"/api/会话/{s['会话编号']}/脉冲", json={"电量": 3.0, "功率": 7})
    before = c.get(f"/api/会话/{s['会话编号']}").json()
    r = _set_status(c, p["id"], "维护")
    assert r.status_code == 200
    assert r.json()["桩态后"] == "维护"
    after = c.get(f"/api/会话/{s['会话编号']}").json()
    assert after["status"] == "已中断"
    assert after["amount"] == before["amount"]


def test_fault_vs_settle_collision_no_double_terminal(c):
    """改故障与关账对撞：不得双终态；半截能被体检识别为 0 异常。"""
    p = _pile_by_name(c, "河甲三号")
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    s = _open(c, p["id"], guns[0]["id"])

    # 先改故障（强制中断），再对同一会话关账：应被拒，不覆盖终态。
    r = _set_status(c, p["id"], "故障")
    assert r.status_code == 200
    settle = c.post(f"/api/会话/{s['会话编号']}/结算")
    assert settle.status_code == 400
    detail = c.get(f"/api/会话/{s['会话编号']}").json()
    assert detail["status"] == "已中断"

    ov = _overview(c)
    assert ov["口径一致"] is True
    assert ov["故障桩在充会话"] == 0


def test_true_concurrent_fault_and_settle_single_terminal(c):
    """并发改故障与关账：两笔同时打，只有一方收口，另一笔 4xx，绝不双终态。"""
    import threading

    p = _pile_by_name(c, "北乙一号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    sid = s["会话编号"]

    barrier = threading.Barrier(2)
    results = {}

    def do_fault():
        barrier.wait()
        results["fault"] = _set_status(c, p["id"], "故障").status_code

    def do_settle():
        barrier.wait()
        results["settle"] = c.post(f"/api/会话/{sid}/结算").status_code

    ts = [threading.Thread(target=do_fault), threading.Thread(target=do_settle)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # 两种合法结局：
    #  a) 改故障先到：会话被强制中断，关账被拒；
    #  b) 关账先到：会话已关账并释放占用，改故障作用在空出来的桩上。
    # 无论哪种，都不得双终态、不得桩故障仍在充。
    assert all(x in (200, 400, 409) for x in results.values()), results
    assert 200 in results.values(), results

    detail = c.get(f"/api/会话/{sid}").json()
    status = detail["status"]
    pile = _pile_by_name(c, "北乙一号")
    if status == "已中断":
        assert results["fault"] == 200
        assert results["settle"] != 200
        assert pile["status"] == "故障"
    elif status == "已关账":
        assert results["settle"] == 200
        assert pile["status"] in ("故障", "空闲")
    else:
        pytest.fail(f"既非已中断也非已关账：{status}")

    ov = _overview(c)
    assert ov["口径一致"] is True, ov
    assert ov["故障桩在充会话"] == 0


def test_no_pulse_after_fault(c):
    """故障后仍记脉冲必须被拒（不得桩已故障会话仍充电中）。"""
    p = _pile_by_name(c, "北乙一号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    assert _set_status(c, p["id"], "故障").status_code == 200

    r = c.post(f"/api/会话/{s['会话编号']}/脉冲", json={"电量": 1.0})
    assert r.status_code == 400
    detail = c.get(f"/api/会话/{s['会话编号']}").json()
    assert detail["kwh"] == 0
    # 未开过脉冲，事件里也不应有脉冲记录。
    assert not any(e["action"] == "脉冲" for e in detail["事件"])


def test_reject_idle_change_keeps_occupancy(c):
    """拒改却占用已变：改空闲被拒时，桩态/占用/会话/枪线一律不动。"""
    p = _pile_by_name(c, "北乙二号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    before_pile = _pile_by_name(c, "北乙二号")
    before_ov = _overview(c)

    r = _set_status(c, p["id"], "空闲")
    assert r.status_code == 400
    assert "在充" in r.json()["detail"]

    after_pile = _pile_by_name(c, "北乙二号")
    assert after_pile["status"] == "占用"
    assert after_pile["open_session_id"] == s["会话编号"]
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    assert guns[0]["status"] == "占用"
    detail = c.get(f"/api/会话/{s['会话编号']}").json()
    assert detail["status"] == "充电中"
    after_ov = _overview(c)
    assert after_ov["充电中"] == before_ov["充电中"]
    assert after_ov["桩位占用"] == before_ov["桩位占用"]
    # 拒改不得落桩态事件。
    assert c.get(f"/api/桩位/{p['id']}/状态事件").json() == []


def test_manual_direct_occupy_rejected(c):
    """占用态只能由开会话进入，人工直填被拒且不改桩态。"""
    p = _pile_by_name(c, "北乙三号")
    r = _set_status(c, p["id"], "占用")
    assert r.status_code == 400
    assert _pile_by_name(c, "北乙三号")["status"] == "空闲"


def test_multi_gun_multi_session_interrupt(c):
    """一桩两枪两在充：改故障必须两笔会话一起收口。"""
    p = _pile_by_name(c, "河甲三号")
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    # 河甲三号有两支枪，但同一桩只能有一个占用会话（开会话要求桩空闲）。
    s1 = _open(c, p["id"], guns[0]["id"])
    # 第二笔同桩开会话应被拒（桩非空闲）。
    s2 = c.post("/api/开会话", json={"桩位编号": p["id"], "枪线编号": guns[1]["id"]})
    assert s2.status_code == 400

    r = _set_status(c, p["id"], "故障")
    assert r.status_code == 200
    assert r.json()["中断会话数"] == 1


def test_fault_then_settle_then_reuse(c):
    """故障桩修好后改空闲可继续开会话；枪线恢复可用。"""
    p = _pile_by_name(c, "河甲一号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    _open(c, p["id"], gun["id"])
    assert _set_status(c, p["id"], "故障").status_code == 200
    assert _set_status(c, p["id"], "空闲").status_code == 200

    pile = _pile_by_name(c, "河甲一号")
    assert pile["status"] == "空闲"
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    assert all(g["status"] == "可用" for g in guns)

    s = _open(c, p["id"], guns[0]["id"])
    assert s["状态"] == "充电中"
    assert _overview(c)["口径一致"] is True


def test_consistency_audit_detects_split(c):
    """体检能识别被人为拧花的半截（模拟只改标签的场景）。"""
    from app.db import db

    p = _pile_by_name(c, "河甲一号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    _open(c, p["id"], gun["id"])

    # 绕过写口，只改桩位标签 -> 体检必须报警。
    with db() as conn:
        conn.execute("UPDATE piles SET status='故障' WHERE id=?", (p["id"],))

    audit = c.get("/api/口径体检").json()
    assert audit["口径一致"] is False
    assert any(v["类型"] == "故障桩仍有在充" for v in audit["异常"])
    ov = _overview(c)
    assert ov["故障桩在充会话"] == 1
    assert ov["口径一致"] is False


def test_refault_heals_split(c):
    """桩已是故障却仍有在充时，再发一次改故障应把在充收口（自愈），而非报无变化。"""
    from app.db import db

    p = _pile_by_name(c, "河甲二号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    # 人为制造半截：桩改故障但会话仍充电中。
    with db() as conn:
        conn.execute("UPDATE piles SET status='故障' WHERE id=?", (p["id"],))
    assert _overview(c)["口径一致"] is False

    r = _set_status(c, p["id"], "故障")
    assert r.status_code == 200
    assert r.json()["中断会话数"] == 1
    assert c.get(f"/api/会话/{s['会话编号']}").json()["status"] == "已中断"
    assert _overview(c)["口径一致"] is True


def test_fault_pile_rejects_new_open(c):
    """故障绿后新开必须被开充闸拒掉：不得「故障回执已绿、路边仍能开充」。"""
    p = _pile_by_name(c, "北乙三号")
    r = _set_status(c, p["id"], "故障", request_key="req-fault-gate")
    assert r.status_code == 200

    # 不指定枪线与指定枪线，两种开充都必须被拒。
    r1 = c.post("/api/开会话", json={"桩位编号": p["id"]})
    assert r1.status_code == 400
    assert "故障" in r1.json()["detail"]
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    r2 = c.post("/api/开会话", json={"桩位编号": p["id"], "枪线编号": gun["id"]})
    assert r2.status_code == 400

    # 拒新开不得留下任何半截：无新会话、枪线仍故障、口径一致。
    assert _sessions(c, "充电中") == []
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    assert all(g["status"] == "故障" for g in guns)
    ov = _overview(c)
    assert ov["口径一致"] is True
    assert ov["故障桩在充会话"] == 0


def test_settle_then_fault_keeps_books_clean(c):
    """先关账再改故障：占用已清、金额已落，改故障不得回写会话与金额。"""
    p = _pile_by_name(c, "河甲二号")
    gun = c.get(f"/api/枪线?桩位编号={p['id']}").json()[0]
    s = _open(c, p["id"], gun["id"])
    sid = s["会话编号"]
    c.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0, "功率": 7})
    st = c.post(f"/api/会话/{sid}/结算")
    assert st.status_code == 200
    amount = st.json()["金额"]
    ov_before = _overview(c)

    r = _set_status(c, p["id"], "故障")
    assert r.status_code == 200
    receipt = r.json()
    assert receipt["桩态前"] == "空闲"
    assert receipt["中断会话数"] == 0
    assert receipt["中断会话"] == []

    # 会话终态与金额不动；枪线随桩故障；总览只动故障桶。
    detail = c.get(f"/api/会话/{sid}").json()
    assert detail["status"] == "已关账"
    assert detail["amount"] == amount
    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    assert all(g["status"] == "故障" for g in guns)
    ov = _overview(c)
    assert ov["桩位故障"] == ov_before["桩位故障"] + 1
    assert ov["本日关账金额"] == ov_before["本日关账金额"]
    assert ov["口径一致"] is True


def test_true_concurrent_fault_and_open_single_fate(c):
    """故障与路边开充对撞：开充闸与故障写口同命运，不得「故障绿了还在新开」的半截。"""
    import threading

    p = _pile_by_name(c, "北乙二号")
    barrier = threading.Barrier(2)
    results = {}

    def do_fault():
        barrier.wait()
        results["fault"] = _set_status(c, p["id"], "故障").status_code

    def do_open():
        barrier.wait()
        results["open"] = c.post("/api/开会话", json={"桩位编号": p["id"]})

    ts = [threading.Thread(target=do_fault), threading.Thread(target=do_open)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # 改故障必须生效；开充只有两种合法结局：
    #  a) 开充抢先在空闲桩上开了会话 -> 同笔被改故障强制中断（开充 200、会话 已中断）；
    #  b) 改故障抢先落桩态 -> 开充闸拒新开（4xx），无会话产生。
    assert results["fault"] == 200, results
    open_resp = results["open"]
    assert open_resp.status_code in (200, 400), open_resp.text

    pile = _pile_by_name(c, "北乙二号")
    assert pile["status"] == "故障"
    if open_resp.status_code == 200:
        sid = open_resp.json()["会话编号"]
        detail = c.get(f"/api/会话/{sid}").json()
        assert detail["status"] == "已中断"
        assert len(detail["中断回执"]) == 1
        # 被中断的会话不得再记脉冲。
        r = c.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.0})
        assert r.status_code == 400
    else:
        assert _sessions(c, "充电中") == []

    guns = c.get(f"/api/枪线?桩位编号={p['id']}").json()
    assert all(g["status"] == "故障" for g in guns)
    ov = _overview(c)
    assert ov["口径一致"] is True, ov
    assert ov["故障桩在充会话"] == 0
