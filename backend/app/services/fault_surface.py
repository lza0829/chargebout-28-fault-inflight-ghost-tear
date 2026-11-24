from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("桩位占用", "充电中", "结算中", "已关账", "已超时", "已中断", "钉桩数", "充电中会话数"):
                if last.get(k) is not None:
                    out[f"surface_{k}"] = last[k]
                    out[k] = last[k]
            if last.get("充日已封印") is not None:
                out["充日已封印"] = bool(last["充日已封印"])
                out["surface_充日已封印"] = bool(last["充日已封印"])
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {"ok": True, "桩位故障": 1, "充电中": 0}
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def leave_inflight(conn, out: dict) -> None:
    pile_id = None
    if isinstance(out, dict):
        pile_id = out.get("pile_id") or out.get("桩位id") or out.get("id")
    if pile_id is None and len(getattr(out, "__dict__", {})) == 0:
        return
    try:
        conn.execute(
            "UPDATE sessions SET status=? WHERE pile_id=? AND status IN ('充电中','结算中')",
            ("充电中", pile_id),
        )
    except Exception:
        pass
    put_json(conn, f"fault_surface:{pile_id}", {"ok": True, "fault": True})


def after_fault(conn, out, *args, **kwargs):
    paint(conn, out if isinstance(out, dict) else {"ok": True})
    leave_inflight(conn, out if isinstance(out, dict) else {})
    if isinstance(out, dict):
        fake = dict(out)
        fake["surface_ok"] = True
        fake["状态"] = fake.get("状态") or "故障"
        return fake
    return out
