(() => {
'use strict';
const C=globalThis.JobReview, F=globalThis.JobReviewFixture, $=s=>document.querySelector(s);
const storage='job-review-desk.v1';
const labels={
 model:'模型后训练',creator:'创作者 AI / Agent 产品',manufacturing:'制造业 AI / 数字化',
 live:'官方曾列出 · 需重核',watch:'观察 / 未核实',inactive:'官方明确不在列',
 listed:'列出',unverified:'无法核验',official_missing:'明确不在列',
 unknown:'未知 / 待补证',stretch:'延伸机会 · 待补证',matched:'已人工核对匹配',not_matched:'已人工核对不符',
 button_only:'只见申请按钮',email_instructions:'列出邮件申请说明',verified_by_user:'人工核对入口可用（非回执）',unverified_endpoint:'入口未核实',
 opened:'已人工打开来源',approved:'已人工批准',chat_started:'已人工开始沟通',resume_sent:'已人工发送简历',platform_confirmed:'已人工核对平台回执',
 mock_send:'模拟发送',mock_ack:'模拟回执'
};
let db={rows:[],profile:'',fixture:false}, writable=true, filter='all', editKey='';
const form=$('#job-form');
function msg(s,error=false){$('#message').textContent=s;$('#message').classList.toggle('error',error);}
try {
 const raw=localStorage.getItem(storage);
 if(raw){const p=JSON.parse(raw);if(Array.isArray(p.rows)&&typeof p.profile==='string') db=p;}
 localStorage.setItem(storage,JSON.stringify(db));
} catch {writable=false;msg('浏览器本地存储不可用。当前页只在内存中使用，请先导出再关闭。',true);}
function persist(){
 try{ if(!writable)return false;localStorage.setItem(storage,JSON.stringify(db));return true; }
 catch {writable=false;return false;}
}
function finish(s){const ok=persist();render();msg(s+(ok?'':' 本地存储不可用，请导出后再关闭。'),!ok);}
function el(tag,t,cls){const n=document.createElement(tag);if(t!==undefined)n.textContent=t;if(cls)n.className=cls;return n;}
function attach(p,...children){p.append(...children);return p;}
function btn(t,cb,secondary=true){const n=el('button',t,secondary?'secondary':'');n.type='button'; n.addEventListener('click',cb);return n;}
function label(t,n){return attach(el('label',t),n);}
function opts(n,values){ n.replaceChildren(...values.map(([value,name])=>{const o=el('option',name);o.value=value;return o;}));return n;}
const select=(vals)=>opts(el('select'),vals.map(x=>[x,labels[x]||x]));
function notice(parent,key,value){parent.append(attach(el('p'),el('strong',key+'：'),document.createTextNode(value||'未知 / 无证据')));}
const formField=n=>form.elements.namedItem(n);
opts($('#track'),[['','自动提示（仅一个方向命中时预选）'],['none','未分流 / 历史记录'],...Object.keys(C.tracks).map(k=>[k,labels[k]])]);
opts($('#observed'),[['','不添加观察'],...C.listing.map(k=>[k,labels[k]])]);
opts($('#intake'),C.intake.map(k=>[k,labels[k]]));
opts($('#eligibility'),C.decisions.map(k=>[k,labels[k]]));
function addRequirement(r={}){
 const wrap=el('div',undefined,'req'), title=el('input'), wording=el('textarea'), stated=el('input'), match=select(C.evidenceStatuses), proof=el('textarea');
 title.value=r.label||'';title.maxLength=120;wording.value=r.wording||'';wording.rows=2;wording.maxLength=2000;
 stated.type='checkbox';stated.checked=!!r.stated;match.value=r.match||'unknown';
 proof.value=r.proof||'';proof.rows=2;proof.maxLength=2000;
 wrap.get=()=>({label:title.value,wording:wording.value,stated:stated.checked,match:match.value,proof:proof.value});
 attach(wrap,label('条款名称',title),attach(el('label',undefined,'check'),stated,document.createTextNode(' JD 明确声明')),label('原文',wording),label('个人证据判断',match),label('个人证据或缺口',proof),btn('移除此条款',()=>wrap.remove()));
 $('#requirements').append(wrap);
}
$('#add-req').onclick=()=>addRequirement();
function reset(){
 editKey='';form.reset();$('#requirements').replaceChildren();$('#form-title').textContent='新增岗位';$('#save').textContent='保存记录';$('#cancel').hidden=true;
}
$('#cancel').onclick=()=>{reset();msg('编辑已取消。');};
function edit(r){
 reset();editKey=r.key;$('#form-title').textContent='编辑岗位';$('#save').textContent='保存修改';$('#cancel').hidden=false;
 for(const k of ['url','employer','roleID','title','jd','postedDate','intake','intakeEvidence','teamGrowth','eligibility','eligibilityReason']) formField(k).value=r[k]||'';
 formField('track').value=r.track||'none';formField('partial').checked=!r.hasJD;
 for(const x of r.requirements)addRequirement(x);
 msg('正在编辑。新的来源观察将追加，原证据与人工日志保留。');$('#form-title').scrollIntoView();
}
form.onsubmit=e=>{
 e.preventDefault();
 try{
  const f=new FormData(form), input=Object.fromEntries(f), previous=db.rows.find(x=>x.key===editKey);
  input.hasJD=!f.has('partial');input.requirements=[...$('#requirements').children].map(x=>x.get());
  input.observations=input.observed?[{state:input.observed,official:f.has('official'),date:input.date,excerpt:input.excerpt}]:[];
  input.tags=previous?.tags||[]; input.notes=previous?.notes||'';
  if(input.observed && !input.url) throw Error('添加来源观察时，请同时提供来源网址。');
  const r=C.createRecord(input), conflict=C.conflicts(db.rows,r).length;
  const duplicate=!editKey&&db.rows.some(x=>x.key===r.key);
  db.rows=C.addRecord(db.rows,r,editKey); if(previous?.fixture) db.rows=db.rows.map(x=>x.key===r.key?{...x,fixture:false}:x);
  reset(); filter='all';
  finish(conflict?'原样保留，发现跨公司姓名/职位 ID 或来源冲突；未合并。':duplicate?'检测到同键，追加来源与版本，原人工记录未覆盖。':r.hasJD?'岗位记录已保存。':'链接已记下，等待本人补 JD；应用没有访问来源。');
 }catch(error){msg(error.message,true);}
};
function renderCard(r){
 const current=C.state(r), title=r.title||'链接记录 · 等待补 JD';
 const card=el('article',undefined,'card');card.dataset.key=r.key;card.dataset.state=current.code;
 attach(card,el('span',labels[current.bucket],`tag track-${current.bucket}`),el('span',labels[r.track]||'未分流','tag'),el('h3',title),el('p',[r.employer,r.roleID,r.postedDate].filter(Boolean).join(' · ')||'身份和发布日期未核实','muted'));
 if(r.fixture)card.append(el('p','公开验收样本 · 冻结观察：'+F.date+' · 非实时状态','muted'));
 if(r.url){
   try{
     const a=el('a','手动打开来源 ↗');a.href=C.normalizeURL(r.url); a.target='_blank'; a.rel='noreferrer noopener';
     card.append(a,el('p','打开链接不会写入任何本地人工进度。','muted'));
   }catch{}
 }
 if(!r.hasJD)card.append(el('p','完整 JD 未核验；此工具不会自动下载或解析来源网页。','muted'));
 notice(card,'入口（不同于岗位状态）',labels[r.intake]+(r.intakeEvidence?' · '+r.intakeEvidence:''));
 notice(card,'候选适配（不计算总分）',labels[r.eligibility]+(r.eligibilityReason?' · '+r.eligibilityReason:''));
 if(r.tags.length)notice(card,'交叉标签',r.tags.map(k=>labels[k]).join('、'));
 if(C.conflicts(db.rows,r).length)card.append(el('p','标识存在跨公司冲突；保持独立，需人工核对。','sim'));
 const detail=attach(el('details'),el('summary','查看证据和来源记录'));
 notice(detail,'团队增长',r.teamGrowth);if(r.notes)notice(detail,'说明',r.notes);
 for(const o of r.observations) notice(detail,`${o.date} · ${o.official?'人工标记官方':'非官方'} · ${labels[o.state]}`,o.excerpt);
 for(const m of r.matches)notice(detail,'关键词提示 · '+labels[m.key],m.evidence);
 for(const x of r.requirements)notice(detail,x.label,`${x.stated?x.wording:'未声明'} ｜ ${labels[x.match]}${x.proof?' · '+x.proof:''}`);
 if(r.jd)notice(detail,'保存的 JD / 摘要',r.jd);
 if(r.aliases?.length)notice(detail,'已保留来源',r.aliases.join('；'));
 if(r.versions?.length){notice(detail,'重复记录版本',String(r.versions.length));for(const v of r.versions)notice(detail,v.url||v.title||'重复版本',v.jd||v.title);}
 card.append(detail,btn('编辑记录',()=>edit(r)));
 const draft=attach(el('details'),el('summary','方向化简历初稿和检查清单'));
 const output=el('textarea');output.rows=10; output.readOnly=true;output.setAttribute('aria-label','本地简历初稿');output.value=C.draft(r,db.profile);
 draft.append(output);card.append(draft);
 const log=attach(el('details'),el('summary','人工进度（自报，不触发平台操作）'));
 const historical=current.bucket!=='live'||!!r.fixture;
 log.append(el('p',historical?'此条为观察、历史或样本，仅可回填历史记录；不能批准、联系、发送或生成实际回执。':'记录的是本人已执行并核实的动作，不是代办。请勿粘贴含个人数据的回执。','muted'));
 for(const l of r.manual)notice(log,labels[l.stage],`${l.at} · ${l.evidence}`);
 const stage=select(historical?['opened']:C.stages), evidence=el('textarea');evidence.rows=2;evidence.maxLength=1500;
 log.append(label('人工动作',stage),label('已亲自核对的证据',evidence),btn('保存自报日志',()=>{
   try {
     const next=C.logManual(r,stage.value,evidence.value);db.rows=db.rows.map(x=>x.key===r.key?next:x);
     finish(next===r?'重复便签已忽略。':'已保存人工记录；没有触发外部动作。');
   }catch(error){msg(error.message,true);}
 }));
 if(r.fixture&&current.bucket==='live'){
   const sim=el('div',undefined,'sim');sim.append(el('p','仅验收样本可用 · 完全模拟 · 不会真实发送'));
   for(const a of r.simulation)notice(sim,labels[a.stage],a.evidence);
   sim.append(btn('模拟发送与回执',()=>{
     db.rows=db.rows.map(x=>x.key===r.key?C.simulate(x):x);finish('本地模拟完成；真实人工日志未变化，没有外部请求。');
   }));
   log.append(sim);
 }
 card.append(log);
 return card;
}
function render(){
 const once=$('#filters');once.replaceChildren();
 const sets=[['all','全部'],['live','列出'],['watch','观察'],['inactive','历史'],...Object.keys(C.tracks).map(k=>[k,labels[k]])];
 for(const [k,t] of sets){const b=btn(t,()=>{filter=k;render();});b.setAttribute('aria-pressed',String(filter===k));once.append(b);}
 const rows=db.rows.filter(r=>filter==='all'||C.state(r).bucket===filter||r.track===filter);
 $('#count').textContent=`${rows.length} / ${db.rows.length}`;
 $('#records').replaceChildren(...(rows.length?rows.map(renderCard):[el('p','还没有此类记录。先添加官方链接，或载入冻结样本。','muted')]));
}
$('#fixtures').onclick=()=>{
 if(!db.fixture){
  for(const sample of F.rows){
   const rec={...C.createRecord(sample),fixture:true};
   if(!db.rows.some(r=>r.key===rec.key))db.rows=C.addRecord(db.rows,rec);
  }
  if(!db.profile){db.profile=F.sampleProfile;$('#profile').value=db.profile;}
  db.fixture=true;
 }
 filter='all';finish('已载入公开岗位样本；虚构履历只用于示范，非用户信息。样本不会覆盖已录入记录。');
};
$('#profile').value=db.profile;
$('#profile-save').onclick=()=>{db.profile=$('#profile').value;finish('素材已存到当前页面的数据集。');};
$('#clear').onclick=()=>{
 if(!window.confirm('清除当前浏览器的全部岗位、日志和简历素材？如需保留请先导出。'))return;
 db={rows:[],profile:'',fixture:false};$('#profile').value='';reset();filter='all';finish('已清除当前浏览器中的记录。');
};
$('#export').onclick=()=>{
 const out=new Blob([JSON.stringify({schema:1,...db},null,2)],{type:'application/json'}), url=URL.createObjectURL(out);
 const a=el('a');a.download='岗位审阅台-本地备份.json';a.href=url;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 msg('已生成本地备份文件；不会上传。备份包含你在本机填写的素材。');
};
render();
})();
