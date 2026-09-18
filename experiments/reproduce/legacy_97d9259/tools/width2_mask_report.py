"""Read-only accounting and screening report for the registered 18-run campaign."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics


def read(p):return json.loads(Path(p).read_text())


def main():
    ap=argparse.ArgumentParser();ap.add_argument('directory',type=Path);args=ap.parse_args();out=args.directory
    reg=read(out/'registration.json');contract=reg['contract'];result=read(out/'campaign_result.json')
    summaries=[];mechanisms=[];errors=[]
    for item in contract['execution_order']:
        for arm in item['arms']:
            d=out/'runs'/(item['workload']+'_'+arm)
            if not (d/'summary.json').exists():continue
            s=read(d/'summary.json');rounds=read(d/'rounds.json');obs=read(d/'observations.json');ledger=read(d/'qcp_ledger.json')
            assert len(ledger)==s['qcp_used']==sum(r['qcp_used'] for r in rounds)
            assert all(r['qcp_used']<=r['qcp_cap'] for r in rounds)
            assert s['total_s']==s['exec_s']+s['rec_s']+s['trans_s']
            assert math.isclose(s['rec_s'],s['explain_s']+s['hypopg_scope_s']+s['other_recommendation_s'],abs_tol=1e-7)
            assert s['exact_restoration']
            w2=[x for x in obs if len(x['index'][1])==2 and x['role']=='ADD']
            full=[x for x in w2 if x['full_window'] and x['status']=='COMPLETE']
            corrected=[x for x in full if x['masked_shadow_delta']<=0<x['raw_delta']]
            mechanism=dict(workload=s['workload'],arm=arm,nominated=len(w2),attempted=sum(x['actual_qcp']>0 for x in w2),
                           complete=sum(x['status']=='COMPLETE' for x in w2),full_complete=len(full),
                           deferred=sum(x['status']=='BUDGET_DEFER' for x in w2),failed=sum(x['status']=='EVALUATION_FAILED' for x in w2),
                           query_mask_hits=sum(x['query_mask_hits'] for x in full),measured_occurrences=sum(x['measured_occurrences'] for x in full),
                           paid_extra_occurrences_vs_same_response_mask=sum(x['measured_occurrences']-x['query_mask_hits'] for x in full),
                           corrected_false_nonbeneficial=len(corrected),in_topk=sum(x['in_topk'] for x in full),
                           after_gate=sum(x['after_gate'] for x in full),materialized=sum(x['materialized_new'] for x in full),
                           corrected_in_topk=sum(x['in_topk'] for x in corrected),corrected_after_gate=sum(x['after_gate'] for x in corrected),
                           corrected_materialized=sum(x['materialized_new'] for x in corrected),
                           stranded_budget_rounds=0)
            for r in rounds:
                deferred=[x for x in obs if x['window']==r['window'] and x['status']=='BUDGET_DEFER']
                assert all(x['required_qcp']>r['remaining_qcp'] for x in deferred)
            mechanisms.append(mechanism);summaries.append(s)
    assert sum(s['qcp_used'] for s in summaries)<=44542
    for filename,rows in [('per_workload_arm.csv',summaries),('width2_mechanisms.csv',mechanisms)]:
        if rows:
            with (out/filename).open('w',newline='',encoding='utf-8') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    by={(s['workload'],s['arm']):s for s in summaries}; comparisons=[]
    for c in contract['cases']:
        if (c['name'],'LQ') in by and (c['name'],'M2Q') in by:
            l,m=by[c['name'],'LQ'],by[c['name'],'M2Q']
            comparisons.append(dict(workload=c['name'],LQ_total_s=l['total_s'],M2Q_total_s=m['total_s'],
                                    relative_change=m['total_s']/l['total_s']-1,extra_rec_s=m['rec_s']-l['rec_s'],
                                    extra_trans_s=m['trans_s']-l['trans_s'],extra_actual_qcp=m['qcp_used']-l['qcp_used']))
    aggregate={}
    for group in ('tpch','tpchs','job','all'):
        vals=[s for s in summaries if group=='all' or s['workload'].split('_')[0]==group]
        aggregate[group]={arm:{k:sum(s[k] for s in vals if s['arm']==arm) for k in ('exec_s','rec_s','trans_s','total_s','qcp_used')}
                          for arm in ('LQ','M2Q')}
    verdict=dict(status=result['status'],completed_runs=len(summaries),completed_rounds=sum(s['rounds'] for s in summaries),
                 accounting='PASS',comparisons=comparisons,aggregate=aggregate,
                 wins=sum(x['relative_change']<0 for x in comparisons),ties=sum(x['relative_change']==0 for x in comparisons),
                 losses=sum(x['relative_change']>0 for x in comparisons),
                 median_relative_change=statistics.median(x['relative_change'] for x in comparisons) if comparisons else None,
                 scope='仅screening/未证明显著性',followup='STOP')
    (out/'screening_verdict.json').write_text(json.dumps(verdict,ensure_ascii=False,indent=2))
    lines=['# Width-2 ADD mask 修复：九负载 screening','',
           f"状态：{result['status']}；{len(summaries)}/18 runs；{verdict['completed_rounds']}/1206 轮。",
           f"Post-fix commit：`{reg['release']['commit']}`；基线：`{contract['policy_baseline_commit']}`。",'',
           '唯一策略差异是默认关闭的 width2_add_full_window。LQ 是共同 QCP 截止包装对照，不冒充历史 Legacy。',
           '主指标 total = execution + recommendation + transition。execution 沿用原 EXPLAIN ANALYZE 的 Plan.Actual Total Time（异常按 30 秒）；另报 execution 墙钟。recommendation 包含安全 scope、校验、日志、初始化及无法细分的管理开销；transition 包含实际 DDL、timeout reset 和最终清理。',
           '所有 full 与 masked shadow 来自本次已付费的同一响应；shadow 不反馈策略、不额外请求。', '',
           '## 九行主表（秒）','',
           '| workload | LQ exec / rec / trans / total | M2Q exec / rec / trans / total | total 变化 |',
           '|---|---:|---:|---:|']
    for c in contract['cases']:
        cells=[]
        for arm in ('LQ','M2Q'):
            s=by.get((c['name'],arm));cells.append(' / '.join(f'{s[k]:.3f}' for k in ('exec_s','rec_s','trans_s','total_s')) if s else '未完成')
        comp=next((x for x in comparisons if x['workload']==c['name']),None)
        lines.append('| '+c['name']+' | '+' | '.join(cells)+' | '+(f"{comp['relative_change']:+.2%}" if comp else '—')+' |')
    lines+=['','## 全量覆盖与预算','',
            '| workload | arm | 轮数 | QCP used/cap | 配置变化轮 | create/drop | timeout | 候选延期 | 原诊断延期 | wall 秒 |',
            '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for s in summaries:
        lines.append(f"| {s['workload']} | {s['arm']} | {s['rounds']} | {s['qcp_used']}/{s['qcp_cap']} | {s['config_change_rounds']} | {s['creates']}/{s['drops']} | {s['timeouts']} | {s['budget_deferred']} | {s['diagnostic_deferred']} | {s['wall_s']:.3f} |")
    lines+=['','## M2Q 二列观测与落地','',
            '| workload | 提名/尝试/完整/延期/失败 | mask/full occurrences | 纠偏 | Top-K/gate后/新建 | 纠偏中 Top-K/gate后/新建 |',
            '|---|---:|---:|---:|---:|---:|']
    for x in mechanisms:
        if x['arm']!='M2Q':continue
        lines.append(f"| {x['workload']} | "+'/'.join(str(x[k]) for k in ('nominated','attempted','full_complete','deferred','failed'))+f" | {x['query_mask_hits']}/{x['measured_occurrences']} | {x['corrected_false_nonbeneficial']} | "+'/'.join(str(x[k]) for k in ('in_topk','after_gate','materialized'))+' | '+'/'.join(str(x[k]) for k in ('corrected_in_topk','corrected_after_gate','corrected_materialized'))+' |')
    lines+=['','## 推荐时间分项（秒，互不重复）','',
            '| workload | arm | EXPLAIN | HypoPG scope（扣除 EXPLAIN） | 其他推荐/校验/记账 |',
            '|---|---|---:|---:|---:|']
    for s in summaries:lines.append(f"| {s['workload']} | {s['arm']} | {s['explain_s']:.3f} | {s['hypopg_scope_s']:.3f} | {s['other_recommendation_s']:.3f} |")
    lines+=['','## 族与总体','', '| 族 | LQ total | M2Q total | 变化 |','|---|---:|---:|---:|']
    for group,a in aggregate.items():
        l,m=a['LQ']['total_s'],a['M2Q']['total_s'];lines.append(f'| {group} | {l:.3f} | {m:.3f} | '+(f'{m/l-1:+.2%}' if l else '—')+' |')
    lines+=['',f"逐 workload 胜/平/负：{verdict['wins']}/{verdict['ties']}/{verdict['losses']}；中位 total 相对变化：{verdict['median_relative_change']:+.2%}。" if comparisons else '无完整配对。',
            '','机械回归单独报告：237 个二列 full、253 个单列 legacy；73 个定向误判恢复。它们不是本次在线覆盖或性能基线。',
            '逐候选原始值、lambda、mu、Top-K/gate/实际新建标记见各 run 的 observations.json。按 occurrence 付费，不引入缓存或跨窗预算。',
            '','**仅screening/未证明显著性**。一次配对不能建立因果独立效应、显著性、长期稳健性或 payback。已停止，不自动追加 repeats、SWAP、exploration 或生产合并。']
    (out/'WIDTH2_MASK_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in verdict.items() if k not in ('aggregate','comparisons')},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
