"""Bounded standard-strategy reports and durable at-most-once daily digest."""
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .tracker import COLUMNS, ACTIVE
from .performance import summarize
from .notifier import send_telegram
from .journal import SCORE_LOW_COHORT

log=logging.getLogger('sentinel.forward')


def summarize_window(trades, scans, start, end):
    # Recommendations grouped by signal date, not actual account executions.
    selected=[t for t in trades if start <= t['sent_at_ms'] < end]
    rows=[dict(t, strategy_version=t['strategy_version']+'/'+
               (t.get('journal_json') or {}).get('strategy_revision','legacy')) for t in selected]
    def grouped(key):
        groups=defaultdict(list)
        for t in rows:
            groups[key(t)].append(t)
        result={}
        for name, group in sorted(groups.items()):
            normalized=[dict(t,strategy_version='ALL',symbol='ALL',side='ALL',
                journal_json=dict(t.get('journal_json') or {},regime='ALL')) for t in group]
            result[name]=summarize(normalized)[0]
        return result
    outcomes=Counter(t['status'] for t in rows)
    decisions=Counter(d['reason'] for s in scans for d in s.get('decisions',[]))
    blocked=Counter({k:v for k,v in decisions.items() if k not in ('sent','paper_recorded')})
    def avg(key):
        values=[t['journal_json'][key] for t in rows if (t.get('journal_json') or {}).get(key) is not None]
        return sum(values)/len(values) if values else None
    return dict(start_ms=start,end_ms=end,paper_only=True,
        scans=len(scans),symbol_evaluations=sum(s['symbols'] for s in scans),
        eligible=sum(s['eligible'] for s in scans),sent=len(rows),
        blocked=dict(blocked),gates=dict(sum((Counter(s.get('reasons',{})) for s in scans),Counter())),
        max_scan_ms=max((s.get('duration_ms',0) for s in scans),default=None),
        failed_symbols=sum(s.get('failed',0) for s in scans),outcomes=dict(outcomes),
        filled=sum(t.get('filled_at_ms') is not None for t in rows),
        no_entry_rate=sum(t['status'] in ('NO_ENTRY','INVALIDATED') for t in rows)/len(rows) if rows else None,
        active=sum(t['status'] in ACTIVE for t in rows),
        ambiguous=sum('AMBIGUOUS' in t['status'] for t in rows),
        tp1_observed=sum(t.get('tp1_at_ms') is not None for t in rows),
        tp2_observed=sum(t.get('tp2_at_ms') is not None for t in rows),
        mean_cost_r=avg('execution_cost_r'),mean_entry_drift_bps=avg('actual_entry_drift_bps'),
        mean_quote_age_ms=avg('quote_age_ms'),mean_candle_age_ms=avg('candle_age_ms'),
        total=grouped(lambda t:'ALL'),by_strategy=grouped(lambda t:t['strategy_version']),
        by_side=grouped(lambda t:t['side']),
        note='Signal-date cohort; active/incomplete excluded. Ambiguity is bounded, never called a win. No profitability claim.')


def score_comparison(trades,threshold=60):
    """Only prospectively tagged ideas; never mix old unfiltered recommendations."""
    selected=[t for t in trades if (t.get('journal_json') or {}).get('notification_policy')=='technical-score-v1'
              and t['journal_json'].get('score_threshold')==threshold]
    groups={}
    for group in ('above','below'):
        rows=[dict(t,strategy_version='ALL',symbol='ALL',side='ALL',
                   journal_json=dict(t['journal_json'],regime='ALL')) for t in selected
              if t['journal_json'].get('score_group')==group]
        groups[group]=summarize(rows)[0] if rows else {}
    return dict(threshold=threshold,groups=groups,
                first_observed_ms=min((t['sent_at_ms'] for t in selected),default=None),
                note='Independent paper ideas, not randomized trials. Silent timestamps are decision times; notified timestamps are send times.')


def add_review_readiness(result,now_ms):
    first=result['first_observed_ms']
    result['elapsed_days']=(now_ms-first)/86400000 if first is not None else 0
    result['review_ready']=(result['elapsed_days']>=7 and result['groups']['above'].get('resolved',0)>=100)
    return result


def read_score_study(conn,now_ms,threshold=60):
    # A bounded 30-day review window; source histories remain durable.
    start=now_ms-30*86400000
    rows=conn.execute('SELECT '+','.join(COLUMNS)+''' FROM scalp_signal_outcomes
        WHERE sent_at_ms >= %s AND sent_at_ms < %s
        AND journal_json->>'notification_policy'='technical-score-v1' ''',(start,now_ms)).fetchall()
    trades=[dict(zip(COLUMNS,row)) for row in rows]
    trades += [r[0] for r in conn.execute('''SELECT trade FROM scalp_shadow_outcomes
        WHERE cohort=%s AND sent_at_ms >= %s AND sent_at_ms < %s''',
        (SCORE_LOW_COHORT,start,now_ms)).fetchall()]
    conn.commit()
    return add_review_readiness(score_comparison(trades,threshold),now_ms)


def read_window(conn,start,end,threshold=60):
    rows=conn.execute('SELECT '+','.join(COLUMNS)+''' FROM scalp_signal_outcomes
        WHERE sent_at_ms >= %s AND sent_at_ms < %s
        AND strategy_version IN ('ema-pullback','breakout-retest')''',(start,end)).fetchall()
    trades=[dict(zip(COLUMNS,row)) for row in rows]
    scans=[r[0] for r in conn.execute('SELECT metrics FROM scalp_standard_scans WHERE observed_ms >= %s AND observed_ms < %s ORDER BY observed_ms',(start,end)).fetchall()]
    shadows=[r[0] for r in conn.execute("SELECT trade FROM scalp_shadow_outcomes WHERE cohort LIKE 'standard-%%' AND sent_at_ms >= %s AND sent_at_ms < %s",(start,end)).fetchall()]
    conn.commit()
    report=summarize_window(trades,scans,start,end)
    low=[t for t in shadows if t.get('journal_json',{}).get('score_group')=='below'
         and t.get('journal_json',{}).get('notification_policy')=='technical-score-v1']
    paired=[t for t in shadows if t not in low]
    report['score_comparison']=score_comparison(trades+low,threshold)
    report['paired_shadows']=summarize([dict(t,symbol='ALL',side='ALL',journal_json=dict(t.get('journal_json') or {},regime='ALL')) for t in paired])
    return report


def format_digest(day, report):
    total=report['total'].get('ALL',{})
    fmt=lambda n: 'henüz yok' if n is None else f'{n:.3f}'
    lines=[f'Scalp Sentinel — {day} günlük paper özeti',
        f"Tarama {report['scans']} | Parite değerlendirmesi {report['symbol_evaluations']}",
        f"Uygun aday {report['eligible']} | Gönderim {report['sent']} | Varsayılan giriş {report['filled']}",
        f"Sonuçlar: {json.dumps(report['outcomes'],ensure_ascii=False)}",
        f"Açık {report['active']} | Belirsiz {report['ambiguous']} | Maliyeti tamamlanan {total.get('resolved',0)}",
        f"Net R (alt sınır): {fmt(total.get('mean_net_r_low'))} | PF: {fmt(total.get('profit_factor_low'))}",
        f"Azami düşüş: {fmt(total.get('max_drawdown_r_low'))}R | Kayıp serisi: {total.get('max_loss_streak',0)}",
        f"Engeller: {json.dumps(report['blocked'],ensure_ascii=False)}"]
    for name,result in report['by_strategy'].items():
        lines.append(f"{name}: {result['signals']} sinyal, {result['resolved']} sonuç, net R {fmt(result['mean_net_r_low'])}")
    comparison=report.get('score_study',report.get('score_comparison',{}))
    if comparison:
        lines.append('Puan karşılaştırması — '+('son 30 gün' if 'score_study' in report else 'rapor dönemi'))
    for group,result in comparison.get('groups',{}).items():
        label='Bildirim alan' if group=='above' else 'Sessiz izlenen'
        filled=result.get('filled',0); total_ideas=result.get('signals',0)
        lines.append(f"{label} (eşik {comparison['threshold']:g}): {total_ideas} fikir, {result.get('resolved',0)} sonuç; "
                     f"giriş {filled}/{total_ideas}, net R {fmt(result.get('mean_net_r_low'))}, PF {fmt(result.get('profit_factor_low'))}")
    if 'review_ready' in comparison:
        lines.append(f"İlk karşılaştırma için 7 gün ve 100 tamamlanmış {comparison['threshold']:g}+ işlem: "+
                     ('hazır (kârlılık kanıtı değildir).' if comparison['review_ready'] else 'henüz tamamlanmadı.'))
    lines.append('Sanal eşit risk hesabı; gerçek hesap getirisi değildir. Açık/eksik sonuç başarı sayılmaz.')
    return '\n'.join(lines)[:3900]


def maintain_reports(runner, now_ms):
    conn=runner.journal.conn
    if conn is None:
        return
    if runner.last_report != now_ms//3600000:
        report=read_window(conn,now_ms-86400000,now_ms,runner.cfg.standard_min_score)
        # Separate bounded log lines remain readable in hosted logs.
        log.info('standard_forward_24h %s',json.dumps({k:v for k,v in report.items() if k!='paired_shadows'},ensure_ascii=False))
        log.info('standard_paired_shadows %s',json.dumps(report['paired_shadows'],ensure_ascii=False))
        log.info('standard_score_study %s',json.dumps(read_score_study(conn,now_ms,runner.cfg.standard_min_score),ensure_ascii=False))
        runner.last_report=now_ms//3600000
    local=datetime.fromtimestamp(now_ms/1000,tz=ZoneInfo(runner.cfg.timezone))
    if local.hour >= runner.cfg.daily_summary_hour:
        end=local.replace(hour=0,minute=0,second=0,microsecond=0)
        start=end-timedelta(days=1)
        day=start.strftime('%Y-%m-%d')+' '+runner.cfg.timezone
        existing=conn.execute('SELECT delivery FROM scalp_daily_reports WHERE report_day=%s',(day,)).fetchone()
        conn.commit()
        if existing is None:
            report=read_window(conn,int(start.timestamp()*1000),int(end.timestamp()*1000),runner.cfg.standard_min_score)
            report['score_study']=read_score_study(conn,now_ms,runner.cfg.standard_min_score)
            status='reserved' if runner.cfg.daily_summary_enabled else 'disabled'
            row=conn.execute('''INSERT INTO scalp_daily_reports VALUES (%s,%s::jsonb,%s,%s)
                ON CONFLICT DO NOTHING RETURNING report_day''',(day,json.dumps(report),now_ms,status)).fetchone()
            conn.commit()
            if row and runner.cfg.daily_summary_enabled:
                try:
                    send_telegram(runner.cfg.telegram_token,runner.cfg.telegram_chat_id,format_digest(day,report))
                    status='sent'
                except Exception as exc:
                    status='uncertain'
                    log.warning('daily_summary_uncertain kind=%s',type(exc).__name__)
                conn.execute('UPDATE scalp_daily_reports SET delivery=%s WHERE report_day=%s',(status,day))
                conn.commit()
                log.info('standard_daily_summary day=%s delivery=%s',day,status)
    if runner.last_prune != now_ms//3600000:
        runner.journal.prune(now_ms,runner.cfg.archive_days)
        runner.last_prune=now_ms//3600000
