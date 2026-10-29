/* Deterministic display geometry. Validated records arrive from V7DashboardData. */
(function(root) {
  'use strict';
  const elapsed=minute=>minute<=690?minute-570:minute-660;
  function timeline(series,width,mode='recorded',left=48,right=12,gap=10) {
    const points=series&&Array.isArray(series.segments)?[].concat(...series.segments):[];
    if(!points.length || points.some(p=>!Number.isFinite(p.minute)||!((p.minute>=570&&p.minute<=690)||(p.minute>=780&&p.minute<=900))))return null;
    const last=points[points.length-1].minute;
    const full=mode==='full',end=full?240:Math.max(1,elapsed(last)),split=full||last>=780;
    const available=Math.max(1,width-left-right-(split?gap:0));
    const x=minute=>left+elapsed(minute)/end*available+(split&&minute>=780?gap:0);
    const candidates=[570,600,630,660,690,780,810,840,870,900].filter(m=>elapsed(m)<=end&&(split||m<=690));
    if(!full&&!candidates.includes(last))candidates.push(last);
    const ticks=[];
    candidates.sort((a,b)=>a-b).forEach(minute=>{
      if(minute===780)return;
      const prior=ticks[ticks.length-1];
      if(prior===undefined||x(minute)-x(prior)>=56)ticks.push(minute);
    });
    const final=full?900:last;
    if(!ticks.includes(final)){
      if(ticks.length>1&&x(final)-x(ticks[ticks.length-1])<48)ticks.pop();
      ticks.push(final);
    }
    return {x,ticks,end,split,last,gap:split?gap:0,breakX:split?x(690)+gap/2:null,mode:full?'full':'recorded'};
  }
  function polarity(value){return typeof value==='number'&&Number.isFinite(value)?value>0?'up':value<0?'down':'neutral':'unknown';}
  function environmentTone(environment){return environment?environment.classification==='bull'?'up':['bear','crisis'].includes(environment.classification)?'down':'neutral':'unknown';}
  function axisLabel(value,range){const digits=range<.35?2:1,rounded=Number(value.toFixed(digits));return (rounded>0?'+':'')+rounded.toFixed(digits)+'%';}
  root.V7ChartMath={timeline,elapsed,polarity,environmentTone,axisLabel};
  if(typeof module!=='undefined')module.exports=root.V7ChartMath;
})(typeof window!=='undefined'?window:globalThis);
