from __future__ import annotations
from datetime import datetime, timedelta
from app.services import fault_surface

from app.db import db, row_dict, rows


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def overview(conn) -> dict:
    from app.services.piles import audit_consistency

    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    # 故障桶与充电中桶同刷：停用桩上的在充会话必为 0，否则口径已拧花。
    fault_open = conn.execute(
        """SELECT COUNT(*) AS n FROM sessions se JOIN piles p ON p.id=se.pile_id
           WHERE p.status IN ('故障','维护') AND se.status IN ('充电中','结算中')"""
    ).fetchone()["n"]
    consistency = audit_consistency(conn)
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "故障桩在充会话": fault_open,
        "口径异常数": consistency["异常数"],
        "口径一致": consistency["口径一致"],
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    where = "WHERE p.site_id=?" if site_id else ""
    params = (site_id,) if site_id else ()
    # 「在充」口径与桩态联动一致：充电中/结算中。
    rs = conn.execute(
        f"""SELECT p.*, s.name AS site_name, r.name AS rate_name,
                   (SELECT se.id FROM sessions se WHERE se.pile_id=p.id
                      AND se.status IN ('充电中','结算中') ORDER BY se.id DESC LIMIT 1) AS open_session_id,
                   (SELECT se.slip_no FROM sessions se WHERE se.pile_id=p.id
                      AND se.status IN ('充电中','结算中') ORDER BY se.id DESC LIMIT 1) AS open_slip_no,
                   (SELECT pe.receipt_no FROM pile_status_events pe WHERE pe.pile_id=p.id
                      ORDER BY pe.id DESC LIMIT 1) AS last_receipt_no
            FROM piles p JOIN sites s ON s.id=p.site_id
            JOIN rate_plans r ON r.id=p.rate_plan_id
            {where} ORDER BY p.id""",
        params,
    ).fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name,
                      p.status AS pile_status, si.receipt_no AS 中断回执号
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               LEFT JOIN (SELECT session_id, MAX(id) AS mid FROM session_interrupts GROUP BY session_id) si_max
                 ON si_max.session_id=se.id
               LEFT JOIN session_interrupts si ON si.id=si_max.mid
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name,
                      p.status AS pile_status, si.receipt_no AS 中断回执号
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               LEFT JOIN (SELECT session_id, MAX(id) AS mid FROM session_interrupts GROUP BY session_id) si_max
                 ON si_max.session_id=se.id
               LEFT JOIN session_interrupts si ON si.id=si_max.mid
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    return rows(rs)


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    d["中断回执"] = rows(
        conn.execute(
            """SELECT si.*, pe.receipt_no AS pile_receipt_no, pe.from_status AS pile_from,
                      pe.to_status AS pile_to
               FROM session_interrupts si
               JOIN pile_status_events pe ON pe.id=si.pile_event_id
               WHERE si.session_id=? ORDER BY si.id""",
            (session_id,),
        ).fetchall()
    )
    return d

_overview_impl = overview


def overview(conn):
    return fault_surface.overlay_overview(_overview_impl(conn), conn)

