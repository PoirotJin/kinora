/* The worker heartbeat and actual output are different facts. */
window.renderProductionHealth=function(data,parentId){
 const parent=document.getElementById(parentId);if(!parent)return;
 let box=document.getElementById(parentId+'-production-health');
 if(!box){box=document.createElement('section');box.id=parentId+'-production-health';box.setAttribute('aria-live','polite');parent.prepend(box);}
 box.replaceChildren();
 const add=(tag,text)=>{const e=document.createElement(tag);e.textContent=text;box.append(e);return e;};
 add('h2','生产健康');
 if(!data?.available){add('p','健康记录暂不可用，不能据此判断生产正常。');return;}
 const when=t=>t?new Date(t*1000).toLocaleString('zh-CN'):'暂无已记录产出';
 const labels={running:'运行中',queued:'等待启动',stopping:'正在停止',stopped:'已停止',completed:'已完成',failed:'失败停止',idle:'未启动'};
 add('p',`${labels[data.status]||data.status} · 当前任务最后入库：${when(data.last_job_saved)} · 全站最后入库：${when(data.last_saved)}`);
 add('p',`连续补词跳过 ${data.consecutive_skips} / ${data.skip_limit} · 连续失败 ${data.consecutive_failures} · 当前重试 ${data.retries} / ${data.retry_limit}`);
 const supply=data.prompt_supply;
 if(supply){
  const states={generating:'正在准备新描述',ready:'新描述已入队',retry_cooling:'补词冷却后重试',buffered_or_cooling:'缓冲已足或主题冷却',paused:'随生产任务暂停',daily_limit:'达到每日调用上限，次日恢复'};
  add('p',`长期供应：${states[supply.status]||supply.status||'等待心跳'} · 今日新增 ${supply.accepted_descriptions||0} 条文本审核合格描述 · 补词步骤 ${supply.steps||0}${supply.max_steps_per_day?' / '+supply.max_steps_per_day:''}`);
  if(supply.rolling_variants)add('p',`每日随机变体轮换已启用 · 每条合格描述每日最多 ${supply.daily_variants_per_description} 次采样 · 目标 ${Number(supply.target_motions_per_day).toLocaleString('zh-CN')} 条生成动作 / 天（含变体，不等于质量审核通过量）`);
 }
 if(data.last_error)add('p','最近异常记录：'+data.last_error);
 for(const a of data.alerts||[])add('p',a.message).className='monitor-alert '+a.severity;
 add('p','入库时间来自持久化提交记录；30 分钟无产出告警。主动停止不会触发停滞告警。').className='muted';
};
