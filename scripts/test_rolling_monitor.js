const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('web/review/index.html','utf8'),nodes=new Map();
class Node{
 constructor(id=''){this.id=id;this.attrs={};this.children=[];this.width=800;this.height=220;this.textContent='';this.className='';this.lines=[];}
 append(...v){this.children.push(...v);}replaceChildren(...v){this.children=v;}setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k];}removeAttribute(k){delete this.attrs[k];}addEventListener(){}
 getContext(){const n=this;return {clearRect(){n.lines=[];},fillText(){},beginPath(){},moveTo(x,y){n.lines.push(['move',x,y]);},lineTo(x,y){n.lines.push(['line',x,y]);},stroke(){},fill(){},arc(){},fillRect(){}};}
}
for(const m of html.matchAll(/id="([^"]+)"/g))nodes.set(m[1],new Node(m[1]));
const $=id=>{assert(nodes.has(id),'Missing HTML node '+id);return nodes.get(id);};
const context=vm.createContext({document:{getElementById:$,createElement:()=>new Node()},window:{},Intl,Date,console});
vm.runInContext(fs.readFileSync('web/review/acceptance.js','utf8'),context);
const render=context.window.MotionFarmRolling.render,now=Date.now()/1000;
const data={available:true,mode:'rolling_24h',status:'rolling',sampled_at:now,campaign:{started_at:now-86400,ends_at:now},production:{available:true,motions:10,motion_seconds:20,hourly:Array.from({length:24},(_,i)=>({started_at:now-86400+i*3600,elapsed_s:3600,motions:i,motion_seconds:i*2}))},reviews:{available:true,reviewed:0},api:{available:true,costs_available:true,calls:2,currencies:{CNY:1.2,USD:.4},unknown_cost_calls:1,total_tokens:30},coverage:{samples:2,expected_samples:1440,coverage_ratio:1/720,gap_seconds:86280},resources:{disk:{},points:[{ts:now-600,cpu:10,gpu:null,rx_bps:10,tx_bps:20},{ts:now-10,cpu:20,gpu:40,rx_bps:20,tx_bps:30}]},stages:[],issues:[],notes:[]};
render(data);assert($.call(null,'acceptance-window').textContent.includes('北京时间'));
assert.equal($('acceptance-report').attrs.href,'/api/monitor/24h/report.md');
assert($('acceptance-status').textContent.includes('持续滚动'));
const texts=$('acceptance-kpis').children.map(n=>n.children.map(x=>x.textContent).join(' ')).join(' ');
assert(texts.includes('待人工审核'));assert(texts.includes('USD'));assert(texts.includes('CNY'));assert(texts.includes('费用未知请求 1'));
// Three grid lines only; separated samples must not gain a connecting data line.
assert.equal($('acceptance-load-chart').lines.filter(x=>x[0]==='line').length,3);
render({...data,sampled_at:now-600});assert($('acceptance-status').textContent.includes('已过期'));assert.equal($('acceptance-report').attrs.href,undefined);
const prior=$('acceptance-kpis').children;
render({available:false,error:'测试缺口'});assert.equal($('acceptance-status').textContent,'测试缺口');assert.strictEqual($('acceptance-kpis').children,prior);
render(data);assert.equal($('acceptance-report').attrs['aria-disabled'],'false');
assert(!nodes.has('acceptance-progress'));assert(!nodes.has('load-chart'));assert(nodes.has('monitor-cost-details'));
console.log('Rolling UI: real HTML bindings, currencies, no-review state, gap separation, stale report, retained snapshot and recovery passed');
