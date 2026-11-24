import { useEffect, useState } from 'react'
import { api } from '../api'

const ACTIONS = ['空闲', '维护', '故障']

const surface_panel = true
export default function Piles() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const [ok, setOk] = useState(false)
  const [receipt, setReceipt] = useState(null)
  const [events, setEvents] = useState(null)
  const [audit, setAudit] = useState(null)

  const load = () => api.桩位().then(setList)
  const loadAudit = () =>
    api
      .口径体检()
      .then(setAudit)
      .catch(() => {})
  useEffect(() => {
    load()
    loadAudit()
  }, [])

  const setStatus = async (p, 状态) => {
    setMsg('')
    setReceipt(null)
    try {
      // 请求号让结局可重放：重试同一请求不会重复中断/写事件。
      const 请求号 = `桩位${p.id}-${状态}-${Date.now()}`
      const r = await api.改桩态(p.id, 状态, { 请求号 })
      setReceipt(r)
      setOk(true)
      const n = r.中断会话数
      setMsg(
        n > 0
          ? `${p.name} → ${状态}，同笔强制中断 ${n} 笔在充会话（回执 ${r.回执号}）`
          : `${p.name} → ${状态}（回执 ${r.回执号}）`,
      )
      await Promise.all([load(), loadAudit()])
    } catch (e) {
      setOk(false)
      setMsg(e.message)
      // 被拒时也回读，确认桩态/占用未被改花。
      load()
    }
  }

  const showEvents = async (id) => {
    setEvents(await api.桩态事件(id))
  }

  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>桩位</h2>
      <p className="lead">
        有在充时改故障或维护，会<b>同笔强制中断</b>该桩的在充会话、释放占用并留可重放回执；
        桩态、会话、占用、枪线与总览同刷。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>站点</th>
              <th>桩名</th>
              <th>额定功率</th>
              <th>费率</th>
              <th>状态</th>
              <th>在充会话</th>
              <th>最近回执</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((p) => (
              <tr key={p.id}>
                <td>{p.site_name}</td>
                <td>{p.name}</td>
                <td>{p.rated_kw}</td>
                <td>{p.rate_name}</td>
                <td>
                  <span className={`tag ${p.status}`}>{p.status}</span>
                </td>
                <td>{p.open_slip_no || '—'}</td>
                <td>{p.last_receipt_no || '—'}</td>
                <td className="row" style={{ border: 0 }}>
                  {ACTIONS.map((s) => (
                    <button key={s} type="button" onClick={() => setStatus(p, s)}>
                      {s}
                    </button>
                  ))}
                  <button type="button" onClick={() => showEvents(p.id)}>
                    事件
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`msg ${ok ? 'ok' : ''}`}>{msg}</p>
      </div>

      {receipt && (
        <div className="panel">
          <h3>
            桩态回执 {receipt.回执号} · {receipt.桩名}：{receipt.桩态前} → {receipt.桩态后}
          </h3>
          <p>
            {receipt.站点} · 占用释放 {receipt.占用释放 ? '是' : '否'} · 中断会话数{' '}
            {receipt.中断会话数}
            {receipt.重放 ? ' · （重放原回执）' : ''}
          </p>
          {(receipt.中断会话 || []).length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>会话号</th>
                  <th>状态前</th>
                  <th>状态后</th>
                  <th>中断回执</th>
                  <th>原因</th>
                  <th>电量</th>
                  <th>金额</th>
                </tr>
              </thead>
              <tbody>
                {(receipt.中断会话 || []).map((s) => (
                  <tr key={s.会话编号}>
                    <td>{s.会话号}</td>
                    <td>{s.会话状态前}</td>
                    <td>
                      <span className={`tag ${s.会话状态后}`}>{s.会话状态后}</span>
                    </td>
                    <td>{s.中断回执}</td>
                    <td>{s.原因}</td>
                    <td>{s.电量}</td>
                    <td>{s.金额}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {events && (
        <div className="panel">
          <h3>桩态事件流水</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>回执</th>
                <th>前</th>
                <th>后</th>
                <th>结果</th>
                <th>中断会话数</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.桩态事件编号}>
                  <td>{e.时间}</td>
                  <td>{e.回执号}</td>
                  <td>{e.桩态前}</td>
                  <td>{e.桩态后}</td>
                  <td>{e.结果}</td>
                  <td>{e.中断会话数}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {audit && (
        <div className="panel">
          <h3>口径体检</h3>
          <p className={audit.口径一致 ? 'ok' : 'msg'}>
            {audit.口径一致
              ? '桩态 / 会话 / 占用 / 枪线 口径一致'
              : `发现 ${audit.异常数} 处口径分裂`}
          </p>
          {!audit.口径一致 && (
            <table>
              <thead>
                <tr>
                  <th>类型</th>
                  <th>桩位</th>
                  <th>会话/枪线</th>
                </tr>
              </thead>
              <tbody>
                {audit.异常.map((v, i) => (
                  <tr key={i}>
                    <td>{v.类型}</td>
                    <td>{v.桩名 || v.桩位编号 || '—'}</td>
                    <td>{v.会话号 || v.枪名 || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
