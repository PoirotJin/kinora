/* Administrator-only monitor data. Costs are configured estimates, not invoices. */
(() => {
  let latest;
  const el=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
  const money=values=>Object.entries(values||{}).map(([c,v])=>`${c} ${Number(v).toFixed(6)}`).join(' + ')||'暂无可计价金额';
  function render(data){
    latest=data;
    let section=document.getElementById('monitor-api-costs');
    if(!section){
      const parent=document.getElementById('monitor');if(!parent)return;
      section=el('section');section.id='monitor-api-costs';section.className='admin-only';
      section.append(el('h2','API 费用构成'));const lifetime=el('div');lifetime.id='api-cost-lifetime';section.append(lifetime);
      const status=el('p');status.id='api-cost-status';status.setAttribute('role','status');section.append(status);const parts=el('p');parts.id='api-cost-components';section.append(parts);
      const label=el('label','费用分组 ');const select=el('select');select.id='api-cost-group';
      for(const [value,text] of [['model','模型'],['kind','调用类型'],['purpose','用途'],['username','用户'],['job_id','任务']]){const option=el('option',text);option.value=value;select.append(option);}
      select.addEventListener('change',()=>render(latest));label.append(select);section.append(label);
      const wrap=el('div');wrap.className='monitor-table';const table=el('table');const head=el('thead');const tr=el('tr');['分组','请求数','已知估算金额（分币种）','费用未知请求'].forEach(t=>tr.append(el('th',t)));head.append(tr);table.append(head);const body=el('tbody');body.id='api-cost-rows';table.append(body);wrap.append(table);section.append(wrap);
      const efficiency=el('div');efficiency.id='api-topic-efficiency';section.append(efficiency);
      const note=el('p');note.id='api-cost-note';note.className='muted';section.append(note);
      const usage=document.getElementById('monitor-usage');const anchor=usage?.closest('.monitor-table');if(anchor)anchor.after(section);else parent.append(section);
    }
    const costs=data?.usage?.costs||data?.costs||data;
    const status=section.querySelector('#api-cost-status'),body=section.querySelector('#api-cost-rows'),note=section.querySelector('#api-cost-note');body.replaceChildren();section.querySelector('#api-cost-lifetime').replaceChildren();section.querySelector('#api-cost-components').textContent='';
    section.querySelector('#api-topic-efficiency').replaceChildren();
    if(!costs?.available){status.textContent='费用统计暂不可用；不能据此判断 API 费用为零。';note.textContent='本地 Agent 的订阅费用不在服务器 API 账本内。';return;}
    const lifetime=section.querySelector('#api-cost-lifetime'),all=costs.lifetime;
    if(all){lifetime.append(el('h3','累计已记录用量按当前单价的参考范围'));lifetime.append(el('p',Object.entries(all.currencies||{}).map(([currency,v])=>`${currency} ${Number(v.minimum).toFixed(4)}–${Number(v.maximum).toFixed(4)}`).join(' + ')||'尚无可计价的累计用量'));
      for(const item of all.models||[]){lifetime.append(el('p',`${item.model} / ${item.kind}：${item.calls} 次请求，${item.currency?`${item.currency} ${Number(item.minimum).toFixed(4)}–${Number(item.maximum).toFixed(4)}（输入 ${Number(item.input_minimum).toFixed(4)}–${Number(item.input_maximum).toFixed(4)}，输出 ${Number(item.output_cost).toFixed(4)}）`:'单价未知'}；${item.unknown_usage} 次用量未知`));}
      const explanation=el('p',`${all.scope} 用量未知 ${all.unknown_usage} 次；单价未知 ${all.unpriced_calls} 次。${all.truncated?'累计模型列表已达上限，范围不完整。':''}`);explanation.className='muted';lifetime.append(explanation);lifetime.append(el('h3','最近请求的费用与归属'));}
    status.textContent=`已知估算：${money(costs.currencies)}；${costs.unknown} / ${costs.calls} 次请求费用未知。${costs.pricing_state==='configured'?'配置版本 '+costs.price_version:costs.pricing_state==='invalid'?'价格配置无效':'尚未配置模型单价'}。`;
    const reasons={unknown_usage:'输入/输出用量缺失',unknown_price:'无匹配的单价',unknown_cached_tokens:'缓存命中 Token 未返回，无法区分输入单价',invalid_cached_tokens:'缓存 Token 数异常'};
    status.textContent+=' '+Object.entries(costs.unknown_reasons||{}).map(([k,v])=>`${reasons[k]||k}：${v} 次`).join('；');
    section.querySelector('#api-cost-components').textContent=Object.entries(costs.components||{}).map(([currency,v])=>`${currency}：未缓存输入 ${Number(v.input||0).toFixed(6)} · 缓存输入 ${Number(v.cached_input||0).toFixed(6)} · 输出 ${Number(v.output||0).toFixed(6)}`).join('；');
    const dim=section.querySelector('select').value;const grouped=new Map();
    for(const item of costs.groups||[]){const key=dim==='model'?`${item.provider||'供应商未记录'} / ${item.model}`:({prompt_generation:'自动提示词生成',prompt_review:'自动文本审核',prompt_rewrite:'用户提示词重写',embedding:'向量索引',chat:'文本模型调用'}[item[dim]]||item[dim]||'未记录');const row=grouped.get(key)||{calls:0,unknown:0,currencies:{}};row.calls+=item.calls;row.unknown+=item.unknown;for(const [currency,value] of Object.entries(item.currencies||{}))row.currencies[currency]=(row.currencies[currency]||0)+value;grouped.set(key,row);}
    for(const [key,row] of [...grouped].sort((a,b)=>b[1].calls-a[1].calls).slice(0,200)){const tr=el('tr');[key,row.calls,money(row.currencies),row.unknown].forEach(t=>tr.append(el('td',String(t))));body.append(tr);}
    const efficiency=section.querySelector('#api-topic-efficiency'),metrics=costs.topic_efficiency;
    efficiency.append(el('h3','按主题：同批次产出与 API 成本'));
    if(!metrics?.available){efficiency.append(el('p',metrics?.reason||'批次成本归属暂不可用'));}
    else{
      efficiency.append(el('p',`${metrics.scope} 未归属请求 ${metrics.unattributed_calls} 次；超出查询范围批次 ${metrics.excluded_runs} 个。`));
      const wrap=el('div');wrap.className='monitor-table';const table=el('table'),head=el('thead'),hr=el('tr');
      ['主题 / 批次','新增文本 / 已生成 / 当前通过','已归属金额','每文本 / 每生成 / 每通过','失败请求开销（已含在总额）','用途明细'].forEach(t=>hr.append(el('th',t)));head.append(hr);table.append(head);const tbody=el('tbody');
      for(const row of metrics.topics||[]){
        const tr=el('tr'),unit=key=>row.per_unit?.[key]?money(row.per_unit[key]):'未知 / 无有效分母';
        const counts=row.output_unknown?'产出记录不完整':`${row.texts} / ${row.motions} / ${row.accepted}`;
        const total=`${money(row.currencies)}；${row.unknown} 次费用未知${row.window_partial?'；窗口截断批次，单位成本不计算':''}`;
        const purposes=Object.entries(row.purposes||{}).map(([name,p])=>`${({prompt_generation:'生成提示词',prompt_review:'文本审核',embedding:'向量化'}[name]||name)}：${p.calls} 次，${money(p.currencies)}，未知 ${p.unknown} 次`).join('；');
        [row.topic+' / '+row.runs,counts,total,['texts','motions','accepted'].map(unit).join(' / '),`${row.failed_calls} 次；${money(row.failed_currencies)}`,purposes].forEach(t=>tr.append(el('td',String(t))));tbody.append(tr);
      }
      table.append(tbody);wrap.append(table);efficiency.append(wrap);
      if(!(metrics.topics||[]).length)efficiency.append(el('p','尚无可归属到自动生产主题的请求。'));
    }
    note.textContent=`${costs.scope} 历史记录 ${costs.historical_calls} 条按当前配置尝试估算，新请求冻结调用时的计价结果。${costs.truncated?'当前记录超过上限，以上不是全历史总价。':''}${grouped.size>200?'仅展示请求数最多的 200 个分组。':''} 本地 Agent 重写不产生服务器重写 API 费用；其订阅/客户端账单不在此统计，后续其他 API 调用仍单独计费。`;
  }
  window.renderApiCosts=render;
})();
