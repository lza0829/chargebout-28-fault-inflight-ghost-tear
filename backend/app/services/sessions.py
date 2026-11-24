from __future__ import annotations
from fastapi import HTTPException
from app.services import fault_surface

from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def release_occupancy(conn, pile_id: int, gun_id: int) -> None:
    """释放占用。桩若已处于故障/维护，不得被会话收口反向改成空闲。"""
    pile = conn.execute("SELECT status FROM piles WHERE id=?", (pile_id,)).fetchone()
    if pile and pile["status"] not in ("故障", "维护"):
        conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (pile_id,))
        conn.execute("UPDATE guns SET status='可用' WHERE id=?", (gun_id,))
    elif pile:
        # 停用桩：枪线保持故障，桩态不动。
        conn.execute("UPDATE guns SET status='故障' WHERE id=?", (gun_id,))


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"锁定电价{price}"),
    )
    return {"会话号": slip, "会话编号": sid, "锁定电价": price, "状态": "充电中"}


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    pile = conn.execute("SELECT status FROM piles WHERE id=?", (se["pile_id"],)).fetchone()
    if pile and pile["status"] in ("故障", "维护"):
        # 兜底：桩已停用不得再进电量（口径不允许「桩已故障却仍在充电中」）。
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    return {"会话编号": session_id, "累计电量": round(new_kwh, 3), "预估金额": amount}


def settle(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    # 条件更新：并发下只有一方能完成 充电中/结算中/已超时 → 已关账，杜绝双终态。
    cur = conn.execute(
        """UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=?
           WHERE id=? AND status IN ('充电中','结算中','已超时')""",
        (amount, ts, price, session_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="会话已被同笔或另一笔操作收口，请刷新")
    release_occupancy(conn, se["pile_id"], se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}"),
    )
    return {"会话编号": session_id, "电量": float(se["kwh"]), "金额": amount, "状态": "已关账"}


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    cur = conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=? AND status='充电中'",
        (f"超过{minutes}分钟未关", session_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="会话已被另一笔操作收口，请刷新")
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟"),
    )
    return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    cur = conn.execute(
        """UPDATE sessions SET status='已中断', closed_at=?, note=?
           WHERE id=? AND status IN ('充电中','结算中','已超时')""",
        (ts, reason, session_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="会话已被另一笔操作收口，请刷新")
    release_occupancy(conn, se["pile_id"], se["gun_id"])
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def force_interrupt_for_pile_event(
    conn, se, *, pile_event_id: int, reason: str, note_fmt: str
) -> str:
    """改故障/维护联动中断：与桩位状态事件同笔提交，落中断回执与会话事件。

    金额不回算（中断不自动结算，金额停留在当前预估），状态收口为 已中断。
    """
    ts = now_str()
    from_status = se["status"]
    cur = conn.execute(
        """UPDATE sessions SET status='已中断', closed_at=?, note=?
           WHERE id=? AND status IN ('充电中','结算中')""",
        (ts, reason, se["id"]),
    )
    if cur.rowcount != 1:
        # 同一桩位事件内不应有并发收口；真发生了就整笔回滚，避免半截。
        raise HTTPException(409, detail=f"会话{se['id']}状态已变，请重试")

    receipt_no = f"中断{pile_event_id}-{se['id']}"
    conn.execute(
        """INSERT INTO session_interrupts(session_id,pile_id,pile_event_id,from_status,to_status,
                                           reason,receipt_no,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (se["id"], se["pile_id"], pile_event_id, from_status, "已中断", reason, receipt_no, ts),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (se["id"], "强制中断", ts, note_fmt),
    )
    conn.execute("UPDATE guns SET status='故障' WHERE id=?", (se["gun_id"],))
    return receipt_no

_set_pile_status_impl = set_pile_status


def set_pile_status(*args, **kwargs):
    out = _set_pile_status_impl(*args, **kwargs)
    return fault_surface.after_fault(args[0] if args else None, out, *args[1:], **kwargs)

