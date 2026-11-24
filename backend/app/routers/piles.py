from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db
from app.services import core, piles as pile_svc

router = APIRouter()


class PileStatusIn(BaseModel):
    状态: str
    请求号: str | None = None
    备注: str = ""


class PileIn(BaseModel):
    站点编号: int
    名称: str = Field(min_length=1)
    额定功率: float = 7
    费率编号: int = 1


@router.get("/桩位")
def list_piles(站点编号: int | None = None):
    with db() as conn:
        return core.list_piles(conn, 站点编号)


@router.post("/桩位")
def create_pile(body: PileIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO piles(site_id,name,rated_kw,status,rate_plan_id) VALUES(?,?,?,?,?)",
            (body.站点编号, body.名称, body.额定功率, "空闲", body.费率编号),
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO guns(pile_id,name,status) VALUES(?,?, '可用')",
            (pid, "主枪"),
        )
        return {"编号": pid, "名称": body.名称}


@router.post("/桩位/{pile_id}/状态")
def change_status(pile_id: int, body: PileStatusIn):
    with db() as conn:
        return pile_svc.set_pile_status(
            conn,
            pile_id,
            body.状态,
            request_key=body.请求号,
            note=body.备注,
        )


@router.get("/桩位/{pile_id}/状态事件")
def pile_events(pile_id: int):
    with db() as conn:
        pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
        if not pile:
            raise HTTPException(404, detail="桩位不存在")
        return pile_svc.list_pile_events(conn, pile_id)


@router.get("/桩态回执")
def status_receipt(请求号: str):
    with db() as conn:
        return pile_svc.get_receipt_by_request(conn, 请求号)


@router.get("/口径体检")
def consistency():
    with db() as conn:
        return pile_svc.audit_consistency(conn)

