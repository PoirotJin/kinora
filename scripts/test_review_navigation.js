// Exercise navigation behavior without a browser or a production service.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('web/review/app.js','utf8');
const start=source.indexOf('async function navigateReview('),end=source.indexOf('\nfunction showReviewConflict',start);
(async()=>{
 const selected=[],messages=[];let allowed=true,loads=0;
 const state={saving:false,items:[{motion_id:'one'}],current:{motion_id:'one'},after:50};
 const context=vm.createContext({state,leave:()=>allowed,selectMotion:async(id,force)=>selected.push([id,force]),notice:m=>messages.push(m),loadList:async append=>{assert.equal(append,true);loads++;state.items.push({motion_id:'two'});state.after=null;}});
 vm.runInContext(source.slice(start,end),context);
 allowed=false;await context.navigateReview(1);assert.equal(loads,0);assert.equal(selected.length,0);
 allowed=true;state.saving=true;await context.navigateReview(1);assert.equal(loads,0);
 state.saving=false;await context.navigateReview(1);assert.equal(loads,1);assert.deepEqual(selected,[['two',true]]);
 state.current={motion_id:'two'};await context.navigateReview(-1);assert.deepEqual(selected[1],['one',true]);
 await context.navigateReview(1);assert.equal(selected.length,2);assert.match(messages[0],/末尾/);
 console.log('review navigation: dirty guard, save guard, pagination, previous and list end passed');
})().catch(e=>{console.error(e);process.exit(1)});
