'use strict';
// Rolling 24h view; legacy acceptance reports remain available via their API.
(function(root){
 const valid=value=>typeof value==='number'&&Number.isFinite(value);
 const number=(value,digits=1)=>valid(value)?value.toLocaleString('zh-CN',{maximumFractionDigits:digits}):'未知';
 const percent=value=>valid(value)?number(value*100)+'%':'未知';
 const duration=seconds=>!valid(seconds)?'未知':seconds>=3600?number(seconds/3600,2)+' 小时':number(seconds/60,2)+' 分钟';
 const money=value=>valid(value)?'¥'+number(value,4):'未知';
 const bytes=value=>valid(value)?number(value/1073741824,2)+' GiB':'未知';
 const stamp=value=>valid(value)?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value*1000)):'未知';
 function reviewText(reviews){
  const r=reviews||{};
  if(r.available===false||!valid(r.reviewed))return '未知';
  return r.reviewed>0?percent(r.pass_rate):'待人工审核';
 }
 function chartRows(items,key){
  return (Array.isArray(items)?items:[]).filter(item=>item&&valid(item.ts)).slice(-1441).map(item=>({time:item.ts,value:valid(item[key])?item[key]:null}));
 }
 const helpers={valid,number,percent,duration,money,reviewText,chartRows};
 if(typeof module!=='undefined'&&module.exports)module.exports=helpers;
 if(typeof document==='undefined')return;
 const $=id=>document.getElementById(id);
 let lastSnapshot=null;
 const stageNames={qwen_generation:'Qwen 文本生成',qwen_review:'Qwen 文本审核',retrieval:'历史相似检索',storage:'结果入库',storage_maintenance:'存储维护',gpu_wait:'等待 GPU',qwen:'Qwen 文本生成',chat:'Qwen 请求',embedding:'文本嵌入',encode:'文本编码',model_load:'模型加载',load:'模型加载',inference:'动作推理',generate:'动作生成',generation:'动作生成',sampling:'动作采样',smpl:'骨架转换',save:'结果入库',write:'结果写入',retarget:'动作重定向',screening:'动作筛查',batch:'完整批次'};
 function label(stage){return stageNames[stage]||String(stage||'未知阶段');}
 function rows(id,values,empty,columns){
  const target=$(id);target.replaceChildren();
  if(!values.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=columns;td.textContent=empty;tr.append(td);target.append(tr);return;}
  for(const valuesRow of values){const tr=document.createElement('tr');for(const value of valuesRow){const td=document.createElement('td');td.textContent=value;tr.append(td);}target.append(tr);}
 }
 function kpis(values){
  const target=$('acceptance-kpis');target.replaceChildren();
  for(const [name,value,note] of values){const article=document.createElement('article'),title=document.createElement('div'),strong=document.createElement('strong'),detail=document.createElement('p');title.textContent=name;strong.textContent=value;detail.textContent=note;article.append(title,strong,detail);target.append(article);}
 }
 function canvas(id,empty){
  const node=$(id),ctx=node.getContext('2d');ctx.clearRect(0,0,node.width,node.height);ctx.font='17px system-ui';ctx.fillStyle='#657080';
  if(empty){ctx.fillText(empty,22,95);node.setAttribute('aria-label',empty);}
  return {node,ctx};
 }
 function chart(id,points,keys,campaign,{disk=false,network=false,gapSeconds=90}={}){
  const items=(Array.isArray(points)?points:[]).filter(p=>p&&valid(p.ts)).slice(-1441),values=items.flatMap(p=>keys.map(key=>p[key])).filter(valid);
  if(!values.length){canvas(id,'暂无可用采样；缺失值不记为零');return;}
  const {node,ctx}=canvas(id),left=64,right=780,bottom=180,top=30;
  const unit=disk?1073741824:network?1048576:1,max=disk||network?Math.max(.01,...values.map(v=>v/unit))*1.08:100;
  const first=items[0].ts,last=items[items.length-1].ts;
  const start=valid(campaign?.started_at)?campaign.started_at:first;
  const end=Math.max(start+60,valid(campaign?.ends_at)?campaign.ends_at:last);
  ctx.fillText(number(max,1)+(disk?' GiB':network?' MiB/s':'%'),8,20);
  ctx.lineWidth=1;ctx.strokeStyle='#e6eaf0';
  for(const fraction of [0,.5,1]){const y=bottom-fraction*(bottom-top);ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();}
  keys.forEach((key,index)=>{
   ctx.strokeStyle=index?'#129a8c':'#0071e3';ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=2.5;ctx.beginPath();let previous=null,tail=null;
   for(const point of items){
    if(!valid(point[key])||point.ts<start||point.ts>end){previous=null;continue;}
    const x=left+(point.ts-start)/(end-start)*(right-left),y=bottom-Math.max(0,Math.min(1,point[key]/unit/max))*(bottom-top);
    if(previous!==null&&point.ts-previous<=gapSeconds)ctx.lineTo(x,y);else ctx.moveTo(x,y);
    previous=point.ts;tail={x,y};
   }
   ctx.stroke();if(tail){ctx.beginPath();ctx.arc(tail.x,tail.y,3,0,Math.PI*2);ctx.fill();}
  });
  ctx.fillStyle='#657080';ctx.fillText(stamp(start),left,211);ctx.textAlign='right';ctx.fillText(stamp(end),right,211);ctx.textAlign='left';
  node.setAttribute('aria-label',disk?'数据集占用采样曲线；当前值与增量见下方文字':network?'最近 24 小时网络吞吐采样曲线；缺口不连接':'CPU 与 GPU 采样曲线；均值见下方文字，缺口不连接');
 }
 function outputChart(hourly){
  const items=(Array.isArray(hourly)?hourly:[]).slice(0,24);
  if(!items.length||!items.some(p=>valid(p.motions)&&valid(p.elapsed_s)&&p.elapsed_s>0)){canvas('acceptance-output-chart','暂无小时产出记录');return;}
  const {node,ctx}=canvas('acceptance-output-chart'),left=56,right=784,bottom=182,top=32;
  const max=Math.max(1,...items.map(p=>valid(p.motions)?p.motions:0)),width=(right-left)/24;
  ctx.fillText(number(max,0)+' 个',6,20);ctx.strokeStyle='#e6eaf0';ctx.beginPath();ctx.moveTo(left,bottom);ctx.lineTo(right,bottom);ctx.stroke();
  items.forEach((point,index)=>{
   const x=left+index*width;
   if(valid(point.motions)&&valid(point.elapsed_s)&&point.elapsed_s>0){ctx.fillStyle='#0071e3';const height=point.motions/max*(bottom-top);ctx.fillRect(x+3,bottom-height,Math.max(2,width-6),Math.max(1,height));}
   else{ctx.fillStyle='#e8edf3';ctx.fillRect(x+3,bottom-2,Math.max(2,width-6),2);}
  });
  ctx.fillStyle='#657080';ctx.fillText(stamp(items[0].started_at),left,212);ctx.textAlign='right';ctx.fillText(stamp(items[items.length-1].started_at+3600),right,212);ctx.textAlign='left';
  node.setAttribute('aria-label','每小时已入库动作数；未到时段与缺失数据没有蓝色柱，下方明细表给出原始数值');
 }
 function reportReady(ready){
  const link=$('acceptance-report');link.setAttribute('aria-disabled',String(!ready));link.tabIndex=ready?0:-1;
  if(ready)link.setAttribute('href','/api/monitor/24h/report.md');else link.removeAttribute('href');
  link.title=ready?'下载最近 24 小时监控的当前快照':'统计就绪后可下载当前快照';
 }
 function render(data){
  if(!data||data.available===false||data.error){
   $('acceptance-status').className='monitor-alert warning';$('acceptance-status').textContent=data?.error||'最近 24 小时数据暂不可用；未将缺失数据计为零。';reportReady(false);return;
  }
  lastSnapshot=data;
  const c=data.campaign||{},p=data.production||{},r=data.reviews||{},a=data.api||{},coverage=data.coverage||{},resources=data.resources||{},disk=resources.disk||{};
  const started=data.mode==='rolling_24h'&&valid(c.started_at)&&valid(c.ends_at);
  const sampleAge=valid(data.sampled_at)?Date.now()/1000-data.sampled_at:null,stale=started&&(sampleAge===null||sampleAge>180);
  $('acceptance-window').textContent=started?stamp(c.started_at)+' — '+stamp(c.ends_at)+' · 北京时间 UTC+8':'最近 24 小时统计正在初始化。';
  const status=!started?'尚未取得滚动统计':stale?'统计已过期，以下为最后取得的记录':'持续滚动 · 每分钟汇总；资源缺失不计作零';
  $('acceptance-status').className=stale?'monitor-alert warning':'muted';$('acceptance-status').textContent=status+(valid(data.sampled_at)?' · 最近汇总 '+stamp(data.sampled_at):'');
  reportReady(started&&!stale);
  const reviewsAvailable=started&&r.available!==false;
  kpis([
   ['已入库动作',started&&p.available!==false?number(p.motions,0):'未知','按最近 24 小时生成窗口统计；'+number(p.motions_per_hour)+' 个 / 小时'],
   ['生成动作时长',started&&p.available!==false?duration(p.motion_seconds):'未知','平均每小时产出 '+duration(p.motion_seconds_per_hour)+' 动作数据'],
   ['人工审核通过率',reviewsAvailable?reviewText(r):'未知',reviewsAvailable?'通过 '+number(r.accepted,0)+' / 已审核 '+number(r.reviewed,0)+'；审核覆盖 '+percent(r.coverage):'尚无可读取的人工审核统计'],
   ['已记录 Token',started&&a.available!==false?number(a.total_tokens,0):'未知','输入 '+number(a.input_tokens,0)+' · 输出 '+number(a.output_tokens,0)+'；用量未知请求 '+number(a.unknown_calls,0)],
   ['已知 API 估算',started&&a.costs_available?(Object.entries(a.currencies||{}).map(([currency,value])=>currency+' '+number(value,4)).join('；')||(a.calls===0?'无已记录请求':'未知')):'未知','费用未知请求 '+number(a.unknown_cost_calls,0)+'；按请求计价记录，非账单'+(a.recording_complete===false?'；记录不完整':'')],
   ['资源采样覆盖',started?percent(coverage.coverage_ratio):'未知','采样 '+number(coverage.samples,0)+' / 应采 '+number(coverage.expected_samples,0)+'；缺口 '+duration(coverage.gap_seconds)]
  ]);
  const sourceNames={production:'自动生产',custom:'定制生产',manual:'手动生产'};
  $('acceptance-sources').textContent=Array.isArray(p.sources)&&p.sources.length?'产出来源：'+p.sources.slice(0,20).map(source=>(sourceNames[source.source]||source.source)+' '+number(source.motions,0)+' 个 / '+duration(source.motion_seconds)).join('；'):'产出包括最近 24 小时内的自动、定制与手动生产；来源明细暂无记录。';
  const textReview=data.text_review||{};
  $('acceptance-text-review').textContent='文本 distinct 判定率：'+(textReview.available===true?percent(textReview.distinct_rate):'未知')+' · distinct '+number(textReview.distinct,0)+' / 审核尝试 '+number(textReview.decisions,0)+'。'+(textReview.scope||'这项指标只统计文字审核，不代表动作质量通过率。');
  const issues=[...(Array.isArray(data.issues)?data.issues:[])];
  if(Array.isArray(a.unpriced_models)&&a.unpriced_models.length)issues.push('以下模型未配置价格：'+a.unpriced_models.join('、'));
  if(valid(coverage.max_gap_s)&&coverage.max_gap_s>180)issues.push('最大采样间隔 '+duration(coverage.max_gap_s)+'，对应区间不能据此确认持续运行状态。');
  const unique=[...new Set(issues.filter(item=>typeof item==='string'&&item.trim()))].slice(0,20),gaps=$('acceptance-gaps');gaps.replaceChildren();
  if(unique.length){const ul=document.createElement('ul');for(const message of unique){const li=document.createElement('li');li.textContent=message;ul.append(li);}gaps.append(ul);}
  outputChart(p.hourly);chart('acceptance-load-chart',resources.points,['cpu','gpu'],c);chart('acceptance-disk-chart',resources.points,['capacity_bytes'],c,{disk:true});chart('acceptance-network-chart',resources.points,['rx_bps','tx_bps'],c,{network:true});
  $('acceptance-load-note').textContent='采样均值：CPU '+(valid(resources.cpu_percent)?number(resources.cpu_percent)+'%':'未知')+' · GPU '+(valid(resources.gpu_percent)?number(resources.gpu_percent)+'%':'未知')+'；只代表取得的样本。';
  $('acceptance-disk-note').textContent='最新占用 '+bytes(disk.latest_bytes)+' · 24 小时占用变化 '+(valid(disk.growth_bytes)&&disk.growth_bytes>0?'+':'')+bytes(disk.growth_bytes)+' · 记录 '+stamp(disk.latest_sampled_at);
  const stages=(Array.isArray(data.stages)?data.stages:[]).slice(0,24),measured=stages.filter(stage=>valid(stage.total_seconds)&&valid(stage.calls)&&stage.calls>0).sort((x,y)=>y.total_seconds-x.total_seconds),unfinished=stages.filter(stage=>stage.calls===0&&stage.unfinished>0);
  $('acceptance-bottleneck').textContent=measured.length?'已测阶段累计耗时最多：'+label(measured[0].stage)+' · '+number(measured[0].total_seconds)+' 秒':'尚无阶段计时，不能判断瓶颈。';
  rows('acceptance-stage-rows',[...measured,...unfinished].map(stage=>[label(stage.stage)+(stage.unfinished>0?'（'+number(stage.unfinished,0)+' 个未结束）':''),number(stage.calls,0),stage.calls>0?number(stage.total_seconds):'未知',number(stage.mean_seconds),number(stage.max_seconds)]),'等待阶段计时数据',5);
  $('acceptance-stage-scope').textContent='阶段记录可能跨任务并行，不能相加作为总墙钟时间。未结束调用不记为成功或 0 秒。失败记录 '+number(stages.length?stages.reduce((sum,stage)=>valid(stage.failures)&&sum!==null?sum+stage.failures:null,0):null,0)+' 次。';
  rows('acceptance-hour-rows',(Array.isArray(p.hourly)?p.hourly:[]).slice(0,24).map(hour=>[stamp(hour.started_at),valid(hour.elapsed_s)&&hour.elapsed_s===0?'未到此时段':duration(hour.elapsed_s),valid(hour.elapsed_s)&&hour.elapsed_s===0?'—':number(hour.motions,0),valid(hour.elapsed_s)&&hour.elapsed_s===0?'—':duration(hour.motion_seconds)]),'尚无小时产出数据',4);
  const notes=[...(Array.isArray(coverage.notes)?coverage.notes:[]),...(Array.isArray(data.notes)?data.notes:[])].filter(note=>typeof note==='string');
  $('acceptance-scope').textContent=[r.denominator||'人工通过率仅针对已审核样本，不代表未审核动作质量。','Token 与 API 费用为最近 24 小时窗口统计；CPU/GPU 为采样均值，非持续测量。',...notes].join(' ');
 }
 function connectionError(message){
  $('acceptance-status').className='monitor-alert warning';$('acceptance-status').textContent='滚动监控连接中断，'+(lastSnapshot?'保留上次记录：':'尚未取得记录：')+String(message||'请稍后刷新');
 }
 const link=$('acceptance-report');if(link)link.addEventListener('click',event=>{if(link.getAttribute('aria-disabled')==='true')event.preventDefault();});
 root.MotionFarmRolling={render,connectionError};
})(typeof window!=='undefined'?window:globalThis);
