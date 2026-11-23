(function (scope) {
  'use strict';
  const tracks = {
    model: {name:'模型后训练', re:/\b(?:sft|post.train(?:ing)?|fine.tun\w*|rlhf|dpo|preference data)\b|后训练|微调|偏好数据/i,
      prompts:['训练目标、基线、数据量和划分','微调方法、消融、评估与失败案例','个人负责的实验和可验证交付']},
    creator: {name:'创作者 AI / Agent 产品', re:/\b(?:creator|creative|agentic|agents?|llm product|generative ai product|product engineer)\b|创作者|创作工具|智能体|生成式产品/i,
      prompts:['目标用户、痛点、产品 PRD 或原型','PoC 到发布的个人贡献与采用证据','实际迭代、质量评估和业务指标']},
    manufacturing: {name:'制造业 AI / 数字化', re:/\b(?:manufactur\w*|factory|industrial|digital twin|mes|oee)\b|制造|工厂|数字孪生|工位/i,
      prompts:['制造场景、流程及指标口径','数据对账、追溯和可视化交付','个人贡献、模拟范围及现场验证缺口']}
  };
  const stages = ['opened','approved','chat_started','resume_sent','platform_confirmed'];
  const listing = ['listed','unverified','official_missing'];
  const intake = ['unknown','button_only','email_instructions','verified_by_user','unverified_endpoint'];
  const evidenceStatuses = ['unknown','matched','not_matched'];
  const decisions = ['unknown','stretch','matched','not_matched'];
  const safe = v => String(v ?? '').trim();
  const identity = v => safe(v).normalize('NFKC').toLowerCase().replace(/\s+/g,' ');
  const invariant = (ok, message) => {if (!ok) throw Error(message);};
  function normalizeURL(value) {
    const raw = safe(value); if (!raw) return '';
    let u; try {u = new URL(raw);} catch {throw Error('请输入完整的 HTTPS 或 HTTP 来源链接。');}
    invariant(['https:', 'http:'].includes(u.protocol) && !u.username && !u.password, '仅允许不含登录信息的 HTTP(S) 链接。');
    const h = u.hostname.toLowerCase().replace(/\.$/,'');
    invariant(h.includes('.') && !/^\d+(?:\.\d+)+$/.test(h) && !/\.(?:localhost|internal|local|test|invalid|home|lan)$/.test(h) && h!=='localhost', '请使用公众可访问机构的域名，不接受本机或内网地址。');
    return u.href;
  }
  function sourceKey(url) {
    if (!url) return '';
    const u = new URL(url);
    u.hash = '';
    for (const key of Array.from(u.searchParams.keys())) if (/^(?:utm_.+|gclid|fbclid)$/i.test(key)) u.searchParams.delete(key);
    u.searchParams.sort();
    u.pathname = u.pathname.replace(/\/$/,'') || '/';
    return u.href;
  }
  function hints(text) {
    const sample=safe(text);
    return Object.keys(tracks).map(key=>{
      const lines=sample.split(/\r?\n/);
      const hit=lines.find(line=>tracks[key].re.test(line));
      return hit ? {key,evidence:hit.slice(0,240)} : null;
    }).filter(Boolean);
  }
  function keyFor(input) {
    const employer=identity(input.employer), id=identity(input.roleID), url=normalizeURL(input.url);
    if (employer && id) return `role:${JSON.stringify([employer,id])}`;
    if (url) return `source:${JSON.stringify([employer,sourceKey(url)])}`;
    invariant(employer && safe(input.title) && safe(input.jd),'无链接的记录需要公司、职位名称和 JD。');
    return `text:${JSON.stringify([employer, identity(input.title)])}`;
  }
  function createRecord(input) {
    const employer=safe(input.employer), roleID=safe(input.roleID), title=safe(input.title), jd=safe(input.jd);
    const url=normalizeURL(input.url);
    const matches=hints([title,jd].filter(Boolean).join('\n'));
    const track=input.track==='none'?'':safe(input.track)|| (matches.length===1?matches[0].key:'');
    invariant(!track || Boolean(tracks[track]),'方向无效。');
    const observations=(input.observations||[]).map(o=>{
      invariant(listing.includes(o.state), '岗位状态无效。');
      const date=safe(o.date), excerpt=safe(o.excerpt);
      invariant(excerpt&&/^\d{4}-\d\d-\d\d$/.test(date), '岗位状态须注明日期及证据。');
      return {state:o.state, official:o.official===true, date, excerpt};
    });
    const applications=safe(input.intake)||'unknown';
    invariant(intake.includes(applications),'申请入口状态无效。');
    invariant(applications==='unknown'||safe(input.intakeEvidence),'申请入口状态需要证据。');
    const eligibility=safe(input.eligibility)||'unknown';
    invariant(decisions.includes(eligibility),'适配状态无效。');
    invariant(eligibility==='unknown'||safe(input.eligibilityReason),'适配判断需要说明。');
    const requirements=(input.requirements||[]).map(r=>{
      const label=safe(r.label), wording=safe(r.wording), stated=r.stated===true, match=safe(r.match)||'unknown', proof=safe(r.proof);
      invariant(label && evidenceStatuses.includes(match), '要求项目或匹配状态无效。');
      invariant(!stated||wording,'已声明的要求需要原文。');
      invariant(match==='unknown'||(stated && proof),'判断匹配或不匹配需要已声明的要求和个人证据。');
      return {label,stated,wording,match,proof};
    });
    return {key:keyFor(input),employer,roleID,title,jd,url,hasJD:input.hasJD!==false&&Boolean(jd),track,
      tags:[...new Set([...(input.tags||[]), ...matches.map(m=>m.key)].filter(t=>t!==track&&tracks[t]))],matches,
      observations,intake:applications,intakeEvidence:safe(input.intakeEvidence),teamGrowth:safe(input.teamGrowth),eligibility,
      eligibilityReason:safe(input.eligibilityReason),requirements,notes:safe(input.notes),postedDate:safe(input.postedDate),manual:[],simulation:[]};
  }
  function state(r) {
    const official=r.observations.filter(o=>o.official && o.state!=='unverified').slice()
       .sort((a,b)=>b.date.localeCompare(a.date) || Number(b.state==='official_missing')-Number(a.state==='official_missing'));
    const last=official[0];
    // A cached listing or timeout cannot override affirmative official evidence.
    if (last?.state==='official_missing') return {bucket:'inactive', code:'inactive_official_missing'};
    const newerUnknown=last && r.observations.some(o=>o.official&&o.state==='unverified'&&o.date>last.date);
    if (!last || newerUnknown) return {bucket:'watch',code:r.hasJD?'watch_unverified':'watch_description_missing'};
    if (!r.hasJD) return {bucket:'watch',code:'watch_description_missing'};
    if (r.eligibility==='stretch') return {bucket:'live',code:'retain_stretch_evidence_pending'};
    if (r.eligibility==='not_matched') return {bucket:'live',code:'retain_listing_requirements_not_matched'};
    return {bucket:'live',code:r.eligibility==='matched'?'retain_candidate_with_evidence':'retain_candidate_with_unknowns'};
  }
  function addRecord(rows, record, existingKey) {
    const existing=rows.find(r=>r.key===(existingKey||record.key));
    invariant(!existingKey||existing,'原记录不存在。');
    invariant(!existingKey||!rows.some(r=>r!==existing&&r.key===record.key),'无法编辑为另一个已存在的岗位标识。');
    invariant(!existingKey || !existing.manual.length || ((!existing.employer || identity(existing.employer)===identity(record.employer)) &&
      (!existing.roleID || identity(existing.roleID)===identity(record.roleID))),
    '有人工日志的记录不得变更已确认的公司或岗位 ID；请新增记录。');
    if (!existing) return [...rows,record];
    const next=existingKey ? {...existing,...record, manual:existing.manual,simulation:existing.simulation,
      observations:unique([...existing.observations,...record.observations]),aliases:unique([...(existing.aliases||[]),existing.url,record.url].filter(Boolean))}
      : {...existing, aliases:unique([...(existing.aliases||[]),existing.url,record.url].filter(Boolean)),
        observations:unique([...existing.observations,...record.observations]),
        versions:unique([...(existing.versions||[]),{url:record.url,title:record.title,jd:record.jd}])};
    return rows.map(r=>r===existing?next:r);
  }
  function unique(arr){return [...new Map(arr.map(v=>[JSON.stringify(v),v])).values()];}
  function conflicts(rows, record) {
    return rows.filter(r=>r.key!==record.key && ((r.roleID&&record.roleID&&identity(r.roleID)===identity(record.roleID))
        || (r.url&&record.url&&sourceKey(r.url)===sourceKey(record.url))) && identity(r.employer)!==identity(record.employer));
  }
  function logManual(r, stage, evidence, at=new Date().toISOString()) {
    const ev=safe(evidence);
    invariant(stages.includes(stage)&&ev,'需要选择人工阶段并填写已亲自核对的证据。');
    invariant(stage==='opened' || state(r).bucket==='live' && !r.fixture,'仅正式、已列出的记录可记录超出查看的人工动作。');
    if (stage==='platform_confirmed') invariant(r.manual.some(a=>a.stage==='resume_sent'),'平台回执必须有人工记录的简历发送在前。');
    if (stage==='resume_sent' || stage==='chat_started') invariant(r.manual.some(a=>a.stage==='approved'),'联系或发送前需要记录人工批准。');
    if (r.manual.some(a=>a.stage===stage && a.evidence===ev))return r;
    return {...r, manual:[...r.manual,{stage,evidence:ev,at,origin:'manual_self_report'}]};
  }
  function simulate(r, at=new Date().toISOString()) {
    invariant(r.fixture===true && state(r).bucket==='live','只允许已列出的冻结验收样本模拟。');
    if (r.simulation.length) return r;
    return {...r,simulation:[{stage:'mock_send',evidence:'模拟环境：未连接任何平台，不发送。',at},{stage:'mock_ack',evidence:'本地生成的模拟回执，不是平台回执。',at}]};
  }
  function draft(r, profile) {
    const p=tracks[r.track];
    if (!p) return '先选择主方向。';
    return ['【初稿 · 仅供人工审核 · 未发送】',`目标：${r.employer||'待补公司'} · ${r.title||'待补职位'}（${p.name}）`,'',
      '候选人提交的素材（未经独立验证，仅在本机暂存）：',safe(profile)||'［待补真实且可验证的个人经历］','',
      '面向岗位需人工写入的证据：',...p.prompts.map(x=>'□ '+x),
      '', '岗位要求复核：',...r.requirements.map(q=>`□ ${q.label}：${q.stated?q.wording:'未声明'}｜${q.match}${q.proof?'｜'+q.proof:''}`),
      '□ 核对地点、语言、工作权与签证；不根据公司/岗位关键词推断个人符合。',
      '□ 核对官方页面和申请入口；删掉无法证明的任职、资历或上线结论。',
      '□ 仅由本人确认、修改，并在目标平台自行处理。'].join('\n');
  }
  const api={tracks,stages,listing,intake,decisions,evidenceStatuses,normalizeURL,sourceKey,hints,createRecord,state,addRecord,conflicts,logManual,simulate,draft};
  if(typeof module!=='undefined') module.exports=api; else scope.JobReview=api;
}(globalThis));
