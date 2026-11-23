(function (scope) {
'use strict';
const date='2026-11-20';
const observed=(state,excerpt,official=true)=>({state,excerpt,official,date});
const req=(label,wording='',proof='')=>({label,wording,stated:!!wording,match:'unknown',proof});
const sampleProfile='【虚构验收素材，并非本人履历】有模型实验/评估案例及 Agent/创作者原型；制造数据对账为模拟。未提供个人贡献、生产交付、全职 PM 年限、语言证据、工作权或搬迁证明。';
const rows=[
{employer:'Gengis',roleID:'Applied-AI-Engineer',title:'Applied AI Engineer',url:'https://gengis.ai/jobs.html',track:'model',tags:['creator'],
 jd:'SFT、训练数据与 benchmarks；YAM 创意 Agent 和评估各占一半。Singapore onsite or remote。',
 observations:[observed('listed','雇主页面列出此岗位及邮件申请说明。')],intake:'email_instructions',intakeEvidence:'页面显示邮件申请说明；未验证送达。',
 eligibilityReason:'有模拟相关案例；实践深度、英语和地区资格不明。',
 requirements:[req('实务','ML、微调/评估、Python 或对应框架实践','模拟实验和评估案例不是独立证明。'),req('英语','强书面及听说能力'),req('学历'),req('最低年限'),req('远程地区与签证')], notes:'不继承同页 Founding Engineer 的 7 年要求。'},
{employer:'Garena',roleID:'J02145119',title:'Garena · J02145119',url:'https://careers.garena.com/global/careers/J02145119',track:'creator',
 jd:'新加坡；LLM/企业 AI 产品从 PoC 到发布或规模化；至少 2 年全职产品、创业或创始人式经历并有 0 到 1 的证据。',
 observations:[observed('listed','雇主页面显示完整 JD 和 Apply Now 按钮。')],intake:'button_only',intakeEvidence:'只有按钮，表单和接收未验证。',
 eligibility:'stretch',eligibilityReason:'模拟原型并不能证明全职年限、0 到 1 或生产交付。',
 requirements:[req('全职经历与 0 到 1','至少 2 年全职产品管理、创业或创始人式经历，并有 0 到 1 记录'),req('AI 产品发布','主导 LLM 或企业 AI 产品，从 PoC 到发布或规模部署','原型不是上线证明。'),req('工作许可与资助')]},
{employer:'KonicaMinolta',roleID:'10_PM30',title:'工厂 DX · 10_PM30',url:'https://hrmos.co/pages/kmi-career/jobs/10_PM30',track:'manufacturing',
 jd:'神户正式职位，现场为主、部分远程；数据利用、系统开发或流程改进经验，Python/SQL 类数据分析基础，推进跨部门项目。',
 observations:[observed('listed','雇主页面列出神户工厂 DX 正式岗位。')],teamGrowth:'雇主称工厂 DX 在提速，不能证明团队实际扩编。',eligibilityReason:'地点、语言、跨部门项目与实际交付待证。',
 requirements:[req('实践经历','数据利用或系统开发或流程改进'),req('数据基础','Python/SQL 等数据处理与分析基础','虚构样本中只有模拟对账。'),req('跨部门推进','推进跨部门项目的经验'),req('制造业硬门槛','','制造业经验欢迎但非强制。'),req('学历或最低年限'),req('日语、工作权或签证')],notes:'所列英语测试非合格线；未经验证可否搬迁、办签证或完成申请。'},
{employer:'NADA-AI',roleID:'Careers/Product-Engineer',title:'Product Engineer',url:'https://nadaai.ai/careers',track:'creator',
 jd:'仅有职位名称和 Singapore 或 Remote；完整 JD 未核验。',hasJD:false,
 observations:[observed('listed','雇主职业页列出职位名称，未显示完整要求。')],intake:'unverified_endpoint',intakeEvidence:'先前入口查询只遇到邮箱防护页面；不据此判关闭。',eligibilityReason:'等待岗位描述与可用入口，不计算适配分。'},
{employer:'37Interactive',roleID:'10808671',title:'历史岗位 · 10808671',url:'https://zhaopin.37.com/index.php?a=post_info&c=society&id=10808671&m=Home',track:'none',
 observations:[observed('official_missing','该岗位不存在！'),observed('listed','第三方缓存副本仍可见，不能覆盖官方明确说明。',false)],eligibilityReason:'保留标识用于去重，不列为活动机会。',notes:'该结果只针对该角色，不能推断停招原因或公司整体招聘。'}
];
const expected=['retain_candidate_with_unknowns','retain_stretch_evidence_pending','retain_candidate_with_unknowns','watch_description_missing','inactive_official_missing'];
const api={date,rows,expected,sampleProfile};
if(typeof module!=='undefined')module.exports=api;else scope.JobReviewFixture=api;
}(globalThis));
