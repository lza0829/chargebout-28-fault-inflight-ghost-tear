import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    api
      .总览()
      .then(setO)
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
  }, [])
  if (!o) return <div className="page"><p className="msg">{err || '加载中'}</p></div>
  const items = [
    ['桩位空闲', o.桩位空闲],
    ['桩位占用', o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
    ['充电中', o.充电中],
    ['结算中', o.结算中],
    ['已关账', o.已关账],
    ['已超时', o.已超时],
    ['已中断', o.已中断],
    ['故障桩在充会话', o.故障桩在充会话],
    ['口径异常数', o.口径异常数],
    ['在充电量', o.在充电量],
    ['本日关账金额', o.本日关账金额],
  ]
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'} ·{' '}
        <span className={o.口径一致 ? 'ok' : 'msg'}>
          口径{o.口径一致 ? '一致' : `分裂（${o.口径异常数}）`}
        </span>
      </p>
      {o.故障桩在充会话 > 0 && (
        <p className="msg">故障/维护桩上仍有 {o.故障桩在充会话} 笔在充，口径已拧花</p>
      )}
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>
      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
