import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  useEffect(() => {
    api.会话().then(setList)
  }, [])
  return (
    <div className="page">
      <h2>会话册</h2>
      <p className="lead">最近会话列表；点开可看脉冲与事件流水。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                  {s.中断回执号 && <div className="sub">{s.中断回执号}</div>}
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => api.会话详情(s.id).then(setDetail)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <p>
            锁定电价 {detail.price_locked} · 电量 {detail.kwh} · 金额 {detail.amount}
          </p>
          {(detail.中断回执 || []).length > 0 && (
            <>
              <h3>中断回执</h3>
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>回执</th>
                    <th>前</th>
                    <th>后</th>
                    <th>原因</th>
                    <th>桩态回执</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.中断回执.map((r) => (
                    <tr key={r.id}>
                      <td>{r.created_at}</td>
                      <td>{r.receipt_no}</td>
                      <td>{r.from_status}</td>
                      <td>{r.to_status}</td>
                      <td>{r.reason}</td>
                      <td>
                        {r.pile_receipt_no}（{r.pile_from} → {r.pile_to}）
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <h3>脉冲</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
