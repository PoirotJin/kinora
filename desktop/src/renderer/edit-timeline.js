'use strict';
// Cuts are [0, frameCount]; playback positions are [0, frameCount - 1].
const EditTimeline = (() => {
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  function range(kind, anchor, at, start, end, frames) {
    if (kind === 'start') return [clamp(at, 0, end - 1), end];
    if (kind === 'end') return [start, clamp(at, start + 1, frames)];
    if (kind === 'move') { const a = clamp(start + at - anchor, 0, frames - (end - start)); return [a, a + end - start]; }
    const a = Math.min(anchor, at, frames - 1);
    return [a, Math.min(frames, Math.max(anchor, at, a + 1))];
  }
  function time(frame, fps) {
    const seconds = Math.max(0, frame / fps);
    return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + (seconds % 60).toFixed(2).padStart(5, '0');
  }
  class Timeline {
    constructor({ seek, changed, locked }) {
      this.seek = seek; this.changed = changed; this.locked = locked;
      this.frames = 0; this.fps = 30; this.frame = 0; this.zoom = 1; this.drag = null;
      this.start = 0; this.end = 0;
      this.track = $('edit-track'); this.scroll = $('edit-track-scroll');
      for (const [id, kind] of [['edit-track','select'],['edit-ruler','seek'],['edit-selection','move'],['edit-trim-start','start'],['edit-trim-end','end'],['edit-playhead','seek']]) {
        const node = $(id);
        node.addEventListener('pointerdown', e => this.begin(e, kind));
        node.addEventListener('pointermove', e => this.move(e));
        node.addEventListener('pointerup', e => this.finish(e));
        node.addEventListener('pointercancel', () => this.cancel());
        node.addEventListener('lostpointercapture', () => this.cancel());
        node.addEventListener('keydown', e => this.key(e, kind));
      }
      $('edit-zoom-in').onclick = () => this.setZoom(this.zoom * 2);
      $('edit-zoom-out').onclick = () => this.setZoom(this.zoom / 2);
      $('edit-zoom-fit').onclick = () => this.setZoom(1);
      $('edit-mark-in').onclick = () => this.mark('start');
      $('edit-mark-out').onclick = () => this.mark('end');
      this.resize = new ResizeObserver(() => this.draw()); this.resize.observe(this.scroll);
      this.sync();
    }
    setClip(clip) {
      this.cancel(); this.reviewRange = null; this.clip = clip; this.frames = clip?.frames || 0; this.fps = clip?.fps || 30;
      this.frame = 0; this.start = 0; this.end = Math.min(300, this.frames); this.zoom = 1;
      $('edit-track-stage').style.width = '100%'; this.scroll.scrollLeft = 0; this.draw();
    }
    setRange(start, end) { this.start = start; this.end = end; this.sync(); }
    valid() { return Number.isInteger(this.start) && Number.isInteger(this.end) && this.start >= 0 && this.end > this.start && this.end <= this.frames; }
    update(start, end) { this.start = start; this.end = end; this.changed(start, end); this.sync(); }
    at(x) { const r = this.track.getBoundingClientRect(); return clamp(Math.round((x - r.left) / (r.width || 1) * this.frames), 0, this.frames); }
    go(frame) { this.seek(clamp(frame, 0, this.frames - 1)); }
    setFrame(frame) {
      this.frame = frame;
      $('edit-playhead').style.left = (this.frames ? frame / this.frames * 100 : 0) + '%';
      $('edit-playhead').setAttribute('aria-valuenow', Math.floor(frame));
      $('edit-clock').textContent = time(frame, this.fps) + ' / ' + time(this.frames, this.fps);
    }
    sync() {
      const valid = this.valid(), editable = valid && this.frames >= 30 && this.end - this.start >= 12 && this.end - this.start <= 300;
      const review = valid && this.reviewRange?.start_frame === this.start && this.reviewRange?.end_frame === this.end && this.end - this.start <= 540;
      const locked = this.locked();
      $('edit-selection').hidden = !valid; $('edit-playhead').hidden = !this.frames; $('edit-track-empty').hidden = !!this.frames;
      $('edit-selection').classList.toggle('invalid', !editable && !review);
      if (valid) { $('edit-selection').style.left = this.start / this.frames * 100 + '%'; $('edit-selection').style.width = (this.end - this.start) / this.frames * 100 + '%'; }
      $('edit-range-label').textContent = valid ? `${time(this.start,this.fps)} — ${time(this.end,this.fps)} · ${((this.end-this.start)/this.fps).toFixed(2)} 秒 · ${this.end-this.start} 帧${editable?'':review?' · 任务过渡区预览；局部修正请另选 12–300 帧':' · 请选择 12–300 帧，原动作至少 30 帧'}` : '载入动作后，在轨道上拖出修正范围';
      $('edit-range-label').classList.toggle('error', !!this.frames && !editable && !review);
      for (const [id, value, min, max] of [['edit-trim-start',this.start,0,this.end-1],['edit-trim-end',this.end,this.start+1,this.frames],['edit-playhead',this.frame,0,this.frames-1]]) {
        const node=$(id);node.setAttribute('aria-valuenow', value);node.setAttribute('aria-valuemin',Math.max(0,min));node.setAttribute('aria-valuemax',Math.max(0,max));node.setAttribute('aria-valuetext',time(value,this.fps));
        node.setAttribute('aria-disabled',String(!this.frames || (id!=='edit-playhead' && locked)));
      }
      for (const id of ['edit-mark-in','edit-mark-out','edit-start','edit-end']) $(id).disabled = !this.frames || locked;
      $('edit-selection').setAttribute('aria-disabled',String(!this.frames || locked));
      $('edit-zoom-in').disabled=!this.frames||this.zoom>=16;$('edit-zoom-out').disabled=!this.frames||this.zoom<=1;$('edit-zoom-fit').disabled=!this.frames;
      $('edit-zoom-label').textContent=this.zoom+'×'; this.setFrame(this.frame);
    }
    begin(e, kind) {
      if (!this.frames || e.button!==0 || e.isPrimary===false || (kind!=='seek' && this.locked())) return;
      e.preventDefault(); e.stopPropagation(); this.cancel();
      const at=this.at(e.clientX), node=e.currentTarget;
      this.drag={kind,node,id:e.pointerId,anchor:at,x:e.clientX,lastX:e.clientX,start:this.start,end:this.end,frame:this.frame,moved:false};
      node.setPointerCapture(e.pointerId);node.focus({preventScroll:true});this.go(this.frame);
      if(kind==='seek')this.go(at);
      this.scrollTick();
    }
    apply(x) {
      const d=this.drag;if(!d)return;
      const at=this.at(x);
      if(d.kind==='seek'){this.go(at);return;}
      if(this.locked()){this.cancel();return;}
      if(d.kind==='select'&&!d.moved)return;
      const [start,end]=range(d.kind,d.anchor,at,d.start,d.end,this.frames);
      this.update(start,end);this.go(d.kind==='end'?end-1:d.kind==='move'?start:at);
    }
    move(e) {const d=this.drag;if(!d||e.pointerId!==d.id)return;e.preventDefault();d.lastX=e.clientX;d.moved ||= Math.abs(e.clientX-d.x)>=3;this.apply(e.clientX);}
    scrollTick() {
      if(!this.drag)return;
      const d=this.drag,r=this.scroll.getBoundingClientRect();
      if(d.moved){const dx=d.lastX<r.left+24?-8:d.lastX>r.right-24?8:0;if(dx){this.scroll.scrollLeft+=dx;this.apply(d.lastX);}}
      this.raf=requestAnimationFrame(()=>this.scrollTick());
    }
    release() {const d=this.drag;this.drag=null;cancelAnimationFrame(this.raf);if(d?.node.hasPointerCapture(d.id))d.node.releasePointerCapture(d.id);}
    finish(e) {const d=this.drag;if(!d||d.id!==e.pointerId)return;if(d.kind==='select'&&!d.moved)this.go(this.at(e.clientX));this.release();}
    cancel() {const d=this.drag;if(!d)return;this.release();this.update(d.start,d.end);this.go(d.frame);}
    mark(kind) {
      if(!this.frames||this.locked())return;
      if(kind==='start'){const start=Math.floor(this.frame);this.update(start,Math.max(start+1,this.end));}
      else {const end=Math.floor(this.frame)+1;this.update(Math.min(this.start,end-1),end);}
    }
    key(e,kind) {
      if (!this.frames || e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();this.cancel();return;}
      if(e.key.toLowerCase()==='i'||e.key.toLowerCase()==='o'){e.preventDefault();e.stopPropagation();this.mark(e.key.toLowerCase()==='i'?'start':'end');return;}
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
      e.preventDefault();e.stopPropagation();if(kind!=='seek'&&kind!=='select'&&this.locked())return;
      const step=(e.shiftKey?10:1)*(e.key==='ArrowLeft'?-1:1), position=(n,lo,hi)=>e.key==='Home'?lo:e.key==='End'?hi:clamp(n+step,lo,hi);
      if(kind==='select'||kind==='seek')this.go(position(Math.floor(this.frame),0,this.frames-1));
      else if(kind==='move'){const start=position(this.start,0,this.frames-(this.end-this.start));this.update(start,start+this.end-this.start);this.go(start);}
      else {const at=kind==='start'?position(this.start,0,this.end-1):position(this.end,this.start+1,this.frames);this.update(...range(kind,0,at,this.start,this.end,this.frames));this.go(kind==='end'?at-1:at);}
    }
    setZoom(value) {
      if(!this.frames)return;this.release();const center=(this.scroll.scrollLeft+this.scroll.clientWidth/2)/(this.scroll.clientWidth*this.zoom||1);
      this.zoom=clamp(value,1,16);$('edit-track-stage').style.width=(this.zoom*100)+'%';this.draw();
      this.scroll.scrollLeft=this.zoom===1?0:center*this.scroll.clientWidth*this.zoom-this.scroll.clientWidth/2;
    }
    draw() {
      this.sync();const ruler=$('edit-ruler'),film=$('edit-filmstrip');ruler.replaceChildren();film.replaceChildren();
      const width=this.track.getBoundingClientRect().width;if(!this.clip||!width)return;
      const marks=Math.min(120,Math.max(2,Math.floor(width/95)));
      for(let i=0;i<=marks;i++){const mark=h('span',{text:time(Math.round(i/marks*this.frames),this.fps)});mark.style.left=i/marks*100+'%';ruler.append(mark);}
      const canvas=h('canvas',{'aria-hidden':'true'});film.append(canvas);MotionStrip.draw(canvas,this.clip,0,this.frames,{top:20,band:18});
    }
  }
  return {Timeline,range,time};
})();
