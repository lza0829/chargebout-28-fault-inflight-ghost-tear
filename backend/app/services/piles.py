"""桩位状态联动口径：有在充时改故障/维护 = 强制中断在充（本仓全局唯一口径）。

不变量（同笔事务内成立，否则整笔回滚）：
- 桩态置于 故障/维护 时，不允许仍有 充电中/结算中 会话——它们必须同笔按中断口径收口；
- 桩态释放占用时，枪线随之释放；
- 每一次桩态变更落一条桩位状态事件（含回执号），被中断的每个会话落一条中断回执；
- 同一请求号重放返回既有回执，不重复写事件。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.config import OPEN_SESSIONS, PILE_STATUSES
from app.db import rows
from app.services.core import now_str
from app.services.sessions import force_interrupt_for_pile_event

# 允许人工设置的目标桩态。占用只能由开会话产生，不接受人工直填。
MANUAL_TARGETS = ("空闲", "故障", "维护")


def sync_pile_guns(conn, pile_id: int) -> None:
    """枪线跟随桩态：空闲→可用；故障/维护→故障；占用→由会话自行占用。"""
    pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        return
    if pile["status"] == "空闲":
        conn.execute("UPDATE guns SET status='可用' WHERE pile_id=?", (pile_id,))
    elif pile["status"] in ("故障", "维护"):
        conn.execute("UPDATE guns SET status='故障' WHERE pile_id=?", (pile_id,))


def _open_sessions(conn, pile_id: int):
    placeholders = ",".join("?" for _ in OPEN_SESSIONS)
    return conn.execute(
        f"""SELECT * FROM sessions WHERE pile_id=? AND status IN ({placeholders})
            ORDER BY id""",
        (pile_id, *OPEN_SESSIONS),
    ).fetchall()


def _event_receipt(conn, ev) -> dict:
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (ev["pile_id"],)).fetchone()
    site = conn.execute("SELECT name FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    intrs = rows(
        conn.execute(
            """SELECT si.*, se.slip_no, se.kwh, se.amount
               FROM session_interrupts si JOIN sessions se ON se.id=si.session_id
               WHERE si.pile_event_id=? ORDER BY si.id""",
            (ev["id"],),
        ).fetchall()
    )
    receipt = {
        "结果": ev["outcome"],
        "回执号": ev["receipt_no"],
        "桩态事件编号": ev["id"],
        "桩位编号": ev["pile_id"],
        "桩名": pile["name"],
        "站点": site["name"] if site else "",
        "桩态前": ev["from_status"],
        "桩态后": ev["to_status"],
        "中断会话数": int(ev["interrupted_count"]),
        "中断会话": [
            {
                "会话编号": r["session_id"],
                "会话号": r["slip_no"],
                "会话状态前": r["from_status"],
                "会话状态后": r["to_status"],
                "中断回执": r["receipt_no"],
                "原因": r["reason"],
                "电量": float(r["kwh"]),
                "金额": float(r["amount"]),
                "时间": r["created_at"],
            }
            for r in intrs
        ],
        "占用释放": ev["to_status"] in ("空闲", "故障", "维护"),
        "时间": ev["created_at"],
        "备注": ev["note"],
    }
    if ev["to_status"] == "故障":
        receipt["故障事件编号"] = ev["id"]
    return receipt


def _stored_receipt(conn, request_key: str, pile_id: int, status: str) -> dict | None:
    ev = conn.execute(
        "SELECT * FROM pile_status_events WHERE request_key=?", (request_key,)
    ).fetchone()
    if not ev:
        return None
    if ev["pile_id"] != pile_id or ev["to_status"] != status:
        raise HTTPException(409, detail="请求号已被另一笔桩态变更占用")
    receipt = _event_receipt(conn, ev)
    receipt["重放"] = True
    return receipt


def get_receipt_by_request(conn, request_key: str) -> dict:
    ev = conn.execute(
        "SELECT * FROM pile_status_events WHERE request_key=?", (request_key,)
    ).fetchone()
    if not ev:
        raise HTTPException(404, detail="无此回执")
    return _event_receipt(conn, ev)


def set_pile_status(
    conn,
    pile_id: int,
    status: str,
    *,
    request_key: str | None = None,
    note: str = "",
) -> dict:
    """改桩态唯一写口。整笔事务由调用方 db() 保证原子性。"""
    if status not in PILE_STATUSES:
        raise HTTPException(400, detail="非法桩位状态")
    if status == "占用":
        raise HTTPException(400, detail="占用态只能由开会话进入，不可人工直填")
    if status not in MANUAL_TARGETS:
        raise HTTPException(400, detail="非法桩位状态")

    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")

    # 同请求号重放：返回原回执，不重复落事件。
    if request_key:
        replay = _stored_receipt(conn, request_key, pile_id, status)
        if replay is not None:
            return replay

    from_status = pile["status"]
    open_sessions = _open_sessions(conn, pile_id)

    if from_status == status and not (open_sessions and status in ("故障", "维护")):
        # 已是目标态且无待heal的在充：仍落审计事件（结果=无变化），不动占用与会话。
        ts = now_str()
        cur = conn.execute(
            """INSERT INTO pile_status_events(pile_id,from_status,to_status,outcome,request_key,
                                              receipt_no,interrupted_count,note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (pile_id, from_status, status, "无变化", request_key or None, None, 0, note, ts),
        )
        event_id = cur.lastrowid
        conn.execute(
            "UPDATE pile_status_events SET receipt_no=? WHERE id=?",
            (f"桩态{event_id}", event_id),
        )
        sync_pile_guns(conn, pile_id)
        ev = conn.execute("SELECT * FROM pile_status_events WHERE id=?", (event_id,)).fetchone()
        receipt = _event_receipt(conn, ev)
        receipt["重放"] = False
        return receipt

    if status == "空闲" and open_sessions:
        # 范围外路径保持原有拒绝口径：不得出现「拒改却占用已变」。
        raise HTTPException(400, detail="仍有在充会话，不可改空闲；请先结算或中断")

    ts = now_str()
    interrupted_count = 0
    outcome = "已生效"

    # 先落桩位状态事件，拿到编号作为中断回执的父。
    cur = conn.execute(
        """INSERT INTO pile_status_events(pile_id,from_status,to_status,outcome,request_key,
                                          receipt_no,interrupted_count,note,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (pile_id, from_status, status, outcome, request_key or None, None, 0, note, ts),
    )
    event_id = cur.lastrowid
    receipt_no = f"桩态{event_id}"
    conn.execute(
        "UPDATE pile_status_events SET receipt_no=? WHERE id=?", (receipt_no, event_id)
    )

    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))

    if status in ("故障", "维护") and open_sessions:
        reason = "桩位改故障强制中断" if status == "故障" else "桩位改维护强制中断"
        for se in open_sessions:
            force_interrupt_for_pile_event(
                conn,
                se,
                pile_event_id=event_id,
                reason=reason,
                note_fmt=f"{reason}（桩态回执{receipt_no}）",
            )
            interrupted_count += 1

    conn.execute(
        "UPDATE pile_status_events SET interrupted_count=? WHERE id=?",
        (interrupted_count, event_id),
    )
    sync_pile_guns(conn, pile_id)

    ev = conn.execute("SELECT * FROM pile_status_events WHERE id=?", (event_id,)).fetchone()
    receipt = _event_receipt(conn, ev)
    receipt["重放"] = False
    return receipt


def list_pile_events(conn, pile_id: int) -> list[dict]:
    evs = conn.execute(
        "SELECT * FROM pile_status_events WHERE pile_id=? ORDER BY id", (pile_id,)
    ).fetchall()
    return [_event_receipt(conn, ev) for ev in evs]


def audit_consistency(conn) -> dict:
    """口径体检：任何一条都表示桩态/会话/占用/枪线互相拧花。"""
    violations: list[dict] = []

    for r in conn.execute(
        """SELECT p.id, p.name, p.status, COUNT(se.id) AS n
           FROM piles p JOIN sessions se ON se.pile_id=p.id
           WHERE p.status IN ('故障','维护') AND se.status IN ('充电中','结算中')
           GROUP BY p.id"""
    ).fetchall():
        violations.append(
            {"类型": "故障桩仍有在充", "桩位编号": r["id"], "桩名": r["name"], "桩态": r["status"], "会话数": r["n"]}
        )

    for r in conn.execute(
        """SELECT p.id, p.name, p.status FROM piles p
           WHERE p.status='占用' AND NOT EXISTS (
             SELECT 1 FROM sessions se WHERE se.pile_id=p.id
               AND se.status IN ('充电中','结算中','已超时'))
        """
    ).fetchall():
        violations.append({"类型": "占用桩无会话", "桩位编号": r["id"], "桩名": r["name"]})

    for r in conn.execute(
        """SELECT se.id, se.slip_no, se.status, p.status AS pile_status, p.name
           FROM sessions se JOIN piles p ON p.id=se.pile_id
           WHERE se.status IN ('充电中','结算中') AND p.status<>'占用'"""
    ).fetchall():
        violations.append(
            {
                "类型": "在充会话桩非占用",
                "会话编号": r["id"],
                "会话号": r["slip_no"],
                "会话状态": r["status"],
                "桩名": r["name"],
                "桩态": r["pile_status"],
            }
        )

    for r in conn.execute(
        """SELECT g.id, g.name, g.status, p.status AS pile_status, p.name AS pile_name
           FROM guns g JOIN piles p ON p.id=g.pile_id
           WHERE g.status='占用' AND NOT EXISTS (
             SELECT 1 FROM sessions se WHERE se.gun_id=g.id
               AND se.status IN ('充电中','结算中','已超时'))"""
    ).fetchall():
        violations.append(
            {"类型": "枪线占用无会话", "枪线编号": r["id"], "枪名": r["name"], "桩名": r["pile_name"]}
        )

    for r in conn.execute(
        """SELECT g.id, g.name, g.status, p.status AS pile_status, p.name AS pile_name
           FROM guns g JOIN piles p ON p.id=g.pile_id
           WHERE p.status IN ('故障','维护') AND g.status='可用'"""
    ).fetchall():
        violations.append(
            {
                "类型": "停用桩枪线仍可用",
                "枪线编号": r["id"],
                "枪名": r["name"],
                "桩名": r["pile_name"],
                "桩态": r["pile_status"],
            }
        )

    return {"口径一致": not violations, "异常数": len(violations), "异常": violations}
