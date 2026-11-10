"""Read-only worker: discover every 5m, monitor candidates on closed 1m bars."""
import copy
import json
import logging
import signal as system_signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from .binance_client import BinanceClient, BinanceError, DataPending
from .config import Config
from .engine import evaluate, prefilter_rejections
from .execution import prepare
from .journal import Journal
from .notifier import format_signal, send_telegram
from .research import candidate_v3, correlated, regime, v2_shadow_variants
from .confluence import detect_confluences, format_confluence_digest
from .price_zones import (detect_price_zones, nearby_zone_alert,
                           format_price_zone_digest, current_price)
from .storage import Cooldowns
from .tracker import track_signals
from .standard import assess as assess_standard

log=logging.getLogger('sentinel')


class Runner:
    def __init__(self,cfg,client=None):
        self.cfg=cfg
        self.client=client or BinanceClient(cfg.base_url)
        # Dry run is strictly read-only, even when DATABASE_URL is present.
        self.storage=Cooldowns('' if cfg.dry_run else cfg.database_url,cfg.dry_run)
        self.journal=Journal(self.storage.conn if self.storage.is_pg else None)
        self.snapshots={}
        self.watch=set()
        self.pending=set()
        self.level_zones={}
        self.last_context_bucket=None
        self.last_long_context_buckets={}
        self.fib_digest_queue={}
        self.last_fib_digest_bucket=int(time.time()*1000)//900000
        self.full_bucket=None
        self.exchange={}
        self.clock_offset=0
        self.last_prune=None
        self.last_report=None

    def close(self):
        self.storage.close()

    def tick(self, now_ms=None):
        cfg=self.cfg
        tick_started=time.monotonic()
        self.journal.alive()  # lost database session also loses the worker lock
        if now_ms is None:
            now_ms=int(time.time()*1000)
        if cfg.primary_strategy=='standard':
            return self.standard_tick(now_ms)
        full=now_ms//300000 != self.full_bucket
        if full:
            self.exchange=self.client.symbol_info()
            remote=int(self.client.get('/fapi/v1/time')['serverTime'])
            if abs(remote-int(time.time()*1000))>30000:
                raise RuntimeError('exchange_clock_skew')
        symbols=[s for s in cfg.symbols if s in self.exchange]
        if not symbols:
            raise RuntimeError('No supported configured symbols')
        # Context-only price zones never force a 1m signal scan of the full
        # universe. Their 15m pass happens after time-sensitive trade delivery.
        wanted=set(symbols) if full else (self.watch | self.pending) & set(symbols)
        wanted.add('BTCUSDT')
        # Open shadow exposures must have current correlation context too.
        for peer in self.journal.active('v3-shadow')+self.journal.active('primary'):
            wanted.add(peer['symbol'])
        candidates={}
        research_by_symbol={}
        confluence_alerts=[]
        level_alerts=[]
        errors=[]
        pending=[]
        data_pending_counts={}
        gate_pass={}
        gate_fail={}
        prefilter_counts={}
        evaluated_sides=0
        primary_eligible=sent=shadow_count=shadow_15m_count=0
        primary_arrivals=[]
        shadow_15m_base=shadow_15m_confirmed=0
        pending_v3=[]
        btc=None

        def assess_v3(sym,snap,tick,side):
            nonlocal primary_eligible,sent,shadow_count
            if not (cfg.shadow_enabled or cfg.primary_strategy=='v3'):
                return
            if "oi5" not in snap or btc is None:
                if "oi5" not in snap:
                    log.info("shadow_candidate_skipped symbol=%s side=%s dataset=oi5",sym,side)
                else:
                    pending_v3.append((sym,snap,tick,side))
                return
            v3=candidate_v3(sym,side,snap,tick,cfg,btc)
            if v3 is None:
                return
            research_by_symbol[sym].append(v3)
            if v3.eligible:
                if cfg.primary_strategy=='v3':
                    primary_arrivals.append((sym,side,v3.score,v3.rvol,int(time.time()*1000)))
                    primary_eligible+=1
                    sent+=self.deliver([v3],'primary',now_ms)
                elif cfg.shadow_enabled:
                    shadow_count+=self.deliver([v3],'v3-shadow',now_ms)

        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(self.client.snapshot,s,now_ms):s
                  for s in sorted(wanted,key=lambda s:(s!='BTCUSDT',s)) if s in self.exchange}
            for future in as_completed(jobs):
                sym=jobs[future]
                stage="snapshot"
                try:
                    snap=future.result()
                    completed_ms=int(time.time()*1000)
                    stage="symbol_metadata"
                    tick=next(f['tickSize'] for f in self.exchange[sym]['filters'] if f['filterType']=='PRICE_FILTER')
                    snap['tick_size']=tick
                    self.snapshots[sym]=snap
                    if sym=='BTCUSDT':
                        btc=snap
                    if snap.get("oi5_pending_reason"):
                        data_pending_counts["oi5"]=data_pending_counts.get("oi5",0)+1
                        log.info("shadow_data_pending symbol=%s dataset=oi5 reason=%s",
                                 sym,snap["oi5_pending_reason"])
                    if sym in symbols:
                        stage="signal_evaluation"
                        evaluated=evaluate(sym,snap,tick,cfg)
                        candidates[sym]=evaluated
                        research_by_symbol[sym]=[]
                        if not evaluated:
                            reasons=prefilter_rejections(snap,cfg) or ["candidate_not_returned"]
                            for reason in reasons:
                                prefilter_counts[reason]=prefilter_counts.get(reason,0)+1
                            log.info("candidate_prefilter_blocked symbol=%s reasons=%s",
                                     sym,json.dumps(reasons,separators=(",",":")))
                        for candidate_signal in evaluated:
                            evaluated_sides+=1
                            failed=[name for name,passed in candidate_signal.checks.items() if not passed]
                            for name,passed in candidate_signal.checks.items():
                                target=gate_pass if passed else gate_fail
                                target[name]=target.get(name,0)+1
                            log.info("candidate_diagnosis symbol=%s side=%s score=%d eligible=%s failed_gates=%s",
                                     sym,candidate_signal.side,candidate_signal.score,
                                     candidate_signal.eligible,json.dumps(failed,separators=(",",":")))
                            s=candidate_signal
                            s.strategy_version='v2.1'
                            s.research=regime(snap)
                            s.research.update({k:snap.get(k) for k in ('flow_source','oi_age_ms','oi5_age_ms')})
                            s.research['snapshot_completed_ms']=completed_ms
                            s.research['evaluation_completed_ms']=int(time.time()*1000)
                            if s.eligible:
                                log.info("eligible_latency symbol=%s side=%s candle_age_ms=%d",
                                         sym,s.side,int(time.time()*1000)-(s.snapshot_ts+60000))
                                stage="signal_delivery"
                                if cfg.primary_strategy=='v2.1':
                                    primary_arrivals.append((sym,s.side,s.score,s.rvol,completed_ms))
                                    primary_eligible+=1
                                    sent+=self.deliver([s],'primary',now_ms)
                                if cfg.shadow_enabled and cfg.primary_strategy=='v2.1':
                                    shadow_15m_base+=1
                                    for variant in v2_shadow_variants(s,snap,now_ms):
                                        research_by_symbol[sym].append(variant)
                                        cohort=variant.strategy_version
                                        count=self.deliver([variant],cohort,now_ms)
                                        if cohort=='v2.1-15m-confirm':
                                            shadow_15m_confirmed+=1
                                            shadow_15m_count+=count
                                    if now_ms//60000 % 15==0:
                                        control=copy.deepcopy(s)
                                        control.strategy_version='v2-control-15m'
                                        control.setup_id=f"{s.setup_id}:15m-sample:{now_ms//900000}"
                                        research_by_symbol[sym].append(control)
                                        self.deliver([control],'v2-control-15m',now_ms)
                            stage="signal_delivery"
                            assess_v3(sym,snap,tick,s.side)
                    if sym=='BTCUSDT' and pending_v3:
                        stage="signal_delivery"
                        delayed,pending_v3=pending_v3,[]
                        for pending_sym,pending_snap,pending_tick,pending_side in delayed:
                            assess_v3(pending_sym,pending_snap,pending_tick,pending_side)
                except DataPending as exc:
                    self.snapshots.pop(sym,None)
                    candidates.pop(sym,None)
                    research_by_symbol.pop(sym,None)
                    pending.append(sym)
                    data_pending_counts[exc.dataset]=data_pending_counts.get(exc.dataset,0)+1
                    log.info('data_pending symbol=%s reason=%s',sym,str(exc))
                except Exception as exc:
                    if stage=="signal_delivery":
                        raise
                    self.snapshots.pop(sym,None)
                    candidates.pop(sym,None)
                    research_by_symbol.pop(sym,None)
                    errors.append(sym)
                    detail=str(exc).replace("\\n"," ")[:160]
                    log.warning('snapshot_failed symbol=%s stage=%s kind=%s detail=%s',
                                sym,stage,type(exc).__name__,detail or 'no_detail')
        signal_ms=int((time.monotonic()-tick_started)*1000)
        # Persist arrival order after time-sensitive notification decisions.
        self.journal.record_arrivals(cfg.primary_strategy,now_ms,primary_arrivals)
        if full:
            self.watch={sym for sym,signals in candidates.items()
                        if any(s.checks['15m trend'] and s.checks['5m structure'] for s in signals)}
            self.full_bucket=now_ms//300000
            self.pending=set(pending+errors)
        else:
            self.pending.difference_update(wanted)
            self.pending.update(pending+errors)
            for sym,signals in candidates.items():
                if any(s.checks['15m trend'] and s.checks['5m structure'] for s in signals):
                    self.watch.add(sym)
                else:
                    self.watch.discard(sym)
        if pending_v3:
            log.info("shadow_data_pending dataset=btc_context candidates=%d",len(pending_v3))
        archive_started=time.monotonic()
        for sym,signals in candidates.items():
            snap=self.snapshots[sym]
            self.journal.archive(sym,snap,now_ms,signals+research_by_symbol[sym])
        # Archive BTC even if it is excluded from the user watchlist.
        if btc and 'BTCUSDT' not in candidates:
            self.journal.archive('BTCUSDT',btc,now_ms,[])
        archive_ms=int((time.monotonic()-archive_started)*1000)
        attempted=len(jobs)
        if errors:
            log.warning("scan_degraded failed=%d attempted=%d",len(errors),attempted)
        if attempted and len(errors)>=attempted:
            raise RuntimeError('all_snapshots_failed')
        # Context is sampled once per 15m and never holds up candidate delivery.
        context_bucket=now_ms//900000
        context_due=full and context_bucket!=self.last_context_bucket
        context_started=time.monotonic()
        if context_due:
            self.last_context_bucket=context_bucket
            for sym in candidates:
                snap=self.snapshots[sym]
                tick=snap['tick_size']
                try:
                    confluence_alerts.extend(detect_confluences(sym,snap,tick))
                except Exception as exc:
                    log.warning("confluence_eval_failed symbol=%s kind=%s",sym,type(exc).__name__)
                try:
                    zones=detect_price_zones(sym,snap,tick)
                    self.level_zones[sym]=zones
                    if zones:
                        self.journal.observe_price_zones(sym,zones,current_price(snap),now_ms)
                        alert=nearby_zone_alert(sym,snap,zones,tick)
                        if alert:
                            level_alerts.append(alert)
                except Exception as exc:
                    log.warning("price_zone_eval_failed symbol=%s kind=%s",sym,type(exc).__name__)
        context_ms=int((time.monotonic()-context_started)*1000)
        context_delivery_started=time.monotonic()
        confluence_sent=self.deliver_confluences(confluence_alerts,now_ms)
        level_sent=self.deliver_price_zones(level_alerts,now_ms)
        context_delivery_ms=int((time.monotonic()-context_delivery_started)*1000)
        log.info('scan_finished mode=%s full=%s scanned=%d watching=%d eligible=%d delivered=%d confluence=%d level_zones=%d shadow_v3=%d shadow_15m_confirm=%d confirm_candidates=%d/%d failed=%d pending=%d',
                 cfg.primary_strategy,full,len(candidates),len(self.watch),primary_eligible,sent,confluence_sent,level_sent,
                 shadow_count,shadow_15m_count,shadow_15m_confirmed,shadow_15m_base,len(errors),len(pending))
        log.info("scan_duration_ms=%d context_due=%s",
                 int((time.monotonic()-tick_started)*1000),context_due)
        log.info("scan_stages_ms signal=%d archive=%d context=%d context_delivery=%d",
                 signal_ms,archive_ms,context_ms,context_delivery_ms)
        log.info("signal_diagnostics symbols=%d side_evaluations=%d prefilter_rejections=%s gates_passed=%s gates_failed=%s data_pending=%s",
                 len(candidates),evaluated_sides,
                 json.dumps(prefilter_counts,sort_keys=True,separators=(",",":")),
                 json.dumps(gate_pass,sort_keys=True,separators=(",",":")),
                 json.dumps(gate_fail,sort_keys=True,separators=(",",":")),
                 json.dumps(data_pending_counts,sort_keys=True,separators=(",",":")))
        # Outcome catch-up follows time-sensitive notifications, never delays
        # entry preparation. A tracking error fails closed on open exposure.
        if not cfg.dry_run:
            track_signals(cfg.database_url,self.client,now_ms=int(time.time()*1000),initialize=False)
            self.journal.track_shadows(self.client,int(time.time()*1000))
            self.journal.settle_primary(self.client,int(time.time()*1000))
        if self.last_prune!=now_ms//3600000:
            self.journal.prune(now_ms,cfg.archive_days)
            self.last_prune=now_ms//3600000
            if self.journal.conn is not None:
                from .report import read_report
                log.info('performance_report %s',json.dumps(read_report(self.journal.conn),ensure_ascii=False))
        return sent

    def standard_tick(self, now_ms):
        """Scan every configured liquid perpetual on each closed 5m candle."""
        bucket=now_ms//300000
        if bucket==self.full_bucket:
            log.info("standard_waiting_next_5m bucket=%d",bucket)
            return 0
        if now_ms%300000>90000:
            self.full_bucket=bucket
            log.info("standard_waiting_next_5m reason=old_closed_bar bucket=%d",bucket)
            return 0
        self.exchange=self.client.symbol_info()
        remote=int(self.client.get('/fapi/v1/time')['serverTime'])
        if abs(remote-int(time.time()*1000))>30000:
            raise RuntimeError('exchange_clock_skew')
        symbols=[s for s in self.cfg.symbols if s in self.exchange
                 and self.exchange[s].get('contractType')=='PERPETUAL']
        if not symbols:
            raise RuntimeError('no_perpetual_symbols')
        eligible=delivered=assessed=0
        failures=[]
        reasons={}
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(self.client.scalp_snapshot,s,now_ms):s for s in symbols}
            for future in as_completed(jobs):
                sym=jobs[future]
                stage='snapshot'
                try:
                    snap=future.result()
                    stage='assessment'
                    tick=next(f['tickSize'] for f in self.exchange[sym]['filters']
                              if f['filterType']=='PRICE_FILTER')
                    signals,prefilter=assess_standard(sym,snap,tick,self.cfg)
                    self.snapshots[sym]=snap
                    for reason in prefilter:
                        reasons[reason]=reasons.get(reason,0)+1
                    for s in signals:
                        assessed+=1
                        failed=[name for name,passed in s.checks.items() if not passed]
                        for reason in failed:
                            reasons[reason]=reasons.get(reason,0)+1
                        log.info("standard_assessment symbol=%s side=%s strategy=%s eligible=%s failed=%s",
                                 sym,s.side,s.strategy_version,s.eligible,json.dumps(failed))
                        if s.eligible:
                            eligible+=1
                            s.research={'snapshot_completed_ms':int(time.time()*1000),
                                        'evaluation_completed_ms':int(time.time()*1000)}
                            stage='delivery'
                            delivered+=self.deliver([s],'primary',now_ms)
                            stage='assessment'
                except DataPending as exc:
                    reasons['data_pending_'+exc.dataset]=reasons.get('data_pending_'+exc.dataset,0)+1
                    failures.append(sym)
                    log.info('standard_data_pending symbol=%s reason=%s',sym,str(exc))
                except Exception as exc:
                    # Delivery/DB failures must stop the worker; never skip an uncertain send.
                    if stage=='delivery':
                        raise
                    if getattr(exc,'sqlstate',None) or type(exc).__module__.startswith('psycopg'):
                        raise
                    failures.append(sym)
                    reasons['snapshot_or_assessment_error']=reasons.get('snapshot_or_assessment_error',0)+1
                    log.warning('standard_symbol_failed symbol=%s kind=%s detail=%s',
                                sym,type(exc).__name__,str(exc)[:140])
        if len(failures)==len(symbols):
            raise RuntimeError('all_standard_snapshots_failed')
        self.full_bucket=bucket
        log.info('standard_scan_finished symbols=%d assessed=%d eligible=%d delivered=%d failed=%d reasons=%s',
                 len(symbols),assessed,eligible,delivered,len(failures),json.dumps(reasons,sort_keys=True))
        # A separate closed-bar pass follows time-sensitive scalp delivery.
        self.long_context_tick(symbols,now_ms)
        if not self.cfg.dry_run:
            track_signals(self.cfg.database_url,self.client,now_ms=int(time.time()*1000),initialize=False)
            self.journal.settle_primary(self.client,int(time.time()*1000))
        return delivered

    def long_context_tick(self,symbols,now_ms):
        """Sample 4h/daily context at UTC close +5m, retrying delayed data."""
        for tf,duration in (("4h",14400000),("1d",86400000)):
            bucket=now_ms//duration
            if (not 300000 <= now_ms%duration < 1200000
                    or self.last_long_context_buckets.get(tf)==bucket):
                continue
            alerts=[]
            failed=0
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs={pool.submit(self.client.context_candles,s,tf,now_ms):s for s in symbols}
                for future in as_completed(jobs):
                    sym=jobs[future]
                    try:
                        rows=future.result()
                        tick=next(f['tickSize'] for f in self.exchange[sym]['filters']
                                  if f['filterType']=='PRICE_FILTER')
                        alerts.extend(detect_confluences(sym,{tf:rows},tick,timeframes=(tf,)))
                    except Exception as exc:
                        failed+=1
                        log.warning('confluence_data_failed symbol=%s timeframe=%s reason=%s',
                                    sym,tf,str(exc)[:160])
            sent=self.deliver_confluences(alerts,now_ms)
            if not failed:
                self.last_long_context_buckets[tf]=bucket
            log.info('long_confluence_scan timeframe=%s scanned=%d failed=%d candidates=%d digests=%d retry=%s',
                     tf,len(symbols),failed,len(alerts),sent,bool(failed))

    def deliver_confluences(self,alerts,now_ms):
        """Batch context observations; never create paper trades."""
        sent=0
        claimed=[]
        for alert in alerts:
            if alert.get('timeframe') not in ('4h','1d'):
                log.info('confluence_short_timeframe_suppressed symbol=%s timeframe=%s',
                         alert.get('symbol'),alert.get('timeframe'))
                continue
            if self.cfg.dry_run:
                log.info("DRY_RUN confluence symbol=%s side=%s timeframe=%s indicators=%s zone_id=%s",
                         alert["symbol"],alert["side"],alert["timeframe"],
                         ",".join(alert["indicators"]),alert["alert_id"])
                continue
            if not self.journal.claim_confluence(alert,now_ms):
                continue
            claimed.append(alert)
        for start in range(0,len(claimed),15):
            group=claimed[start:start+15]
            try:
                send_telegram(self.cfg.telegram_token,self.cfg.context_chat_id or self.cfg.telegram_chat_id,
                              format_confluence_digest(group,self.cfg.timezone))
            except Exception as exc:
                for alert in group:
                    self.journal.confluence_delivery(alert,"uncertain")
                log.warning("confluence_delivery_uncertain items=%d kind=%s",
                            len(group),type(exc).__name__)
            else:
                for alert in group:
                    self.journal.confluence_delivery(alert,"sent")
                log.info("confluence_digest_sent items=%d",len(group))
                sent+=1
        return sent

    def deliver_price_zones(self,alerts,now_ms):
        """Throttle strong zone alerts and batch standalone Fib levels every 15 minutes."""
        sent=0
        immediate=sorted((a for a in alerts if a.get("strength")!="digest"),
                         key=lambda a:(-len(a["sources"]),a["distance"]))
        digests=[a for a in alerts if a.get("strength")=="digest"]
        for alert in digests:
            self.fib_digest_queue[alert["alert_id"]]=alert
        claimed=[]
        for alert in immediate:
            if self.journal.recent_price_zone_count(now_ms)>=3:
                log.info("price_zone_rate_limited symbol=%s window=15m cap=3",alert["symbol"])
                continue
            if self.cfg.dry_run:
                log.info("DRY_RUN price_zone symbol=%s center=%s sources=%s zone_id=%s",
                         alert["symbol"],alert["center"],",".join(alert["sources"]),alert["alert_id"])
                continue
            if not self.journal.claim_price_zone(alert,now_ms):
                continue
            claimed.append(alert)
        if claimed:
            try:
                send_telegram(self.cfg.telegram_token,self.cfg.context_chat_id or self.cfg.telegram_chat_id,
                              format_price_zone_digest(claimed,self.cfg.timezone,"Strong zones"))
            except Exception as exc:
                for alert in claimed:
                    self.journal.price_zone_delivery(alert,"uncertain")
                log.warning("price_zone_delivery_uncertain items=%d kind=%s",
                            len(claimed),type(exc).__name__)
            else:
                for alert in claimed:
                    self.journal.price_zone_delivery(alert,"sent")
                log.info("price_zone_digest_sent items=%d",len(claimed))
                sent+=1

        bucket=now_ms//900000
        if bucket>self.last_fib_digest_bucket:
            self.last_fib_digest_bucket=bucket
            queued=list(self.fib_digest_queue.values())
            self.fib_digest_queue.clear()
            eligible=[]
            for alert in queued:
                alert["alert_type"]="digest"
                if self.cfg.dry_run:
                    eligible.append(alert)
                elif self.journal.claim_price_zone(alert,now_ms):
                    eligible.append(alert)
            if eligible:
                if self.cfg.dry_run:
                    log.info("DRY_RUN price_zone_digest zones=%d",len(eligible))
                else:
                    try:
                        send_telegram(self.cfg.telegram_token,self.cfg.context_chat_id or self.cfg.telegram_chat_id,
                                      format_price_zone_digest(eligible,self.cfg.timezone))
                    except Exception as exc:
                        for alert in eligible:
                            self.journal.price_zone_delivery(alert,"uncertain")
                        log.warning("price_zone_digest_delivery_uncertain zones=%d kind=%s",
                                    len(eligible),type(exc).__name__)
                    else:
                        for alert in eligible:
                            self.journal.price_zone_delivery(alert,"sent")
                        log.info("price_zone_digest_sent zones=%d",len(eligible))
                        sent+=1
        return sent

    def deliver(self,signals,cohort,now_ms):
        cfg=self.cfg
        delivery_started_ms=int(time.time()*1000)
        if cohort=='primary' and self.journal.pending_delivery():
            log.error('delivery_uncertain_primary_paused reconciliation_required=true')
            return 0
        peers=self.journal.active(cohort)
        sent=0
        recent=self.journal.recent_count(cohort,delivery_started_ms)
        for s in sorted(signals,key=lambda s:(s.score,s.rvol),reverse=True):
            if sent+recent>=cfg.max_signals_per_run or len(peers)>=cfg.max_open_signals:
                self.journal.decision(s,cohort,delivery_started_ms,'exposure_or_rate_limit')
                continue
            if any(p['symbol']==s.symbol for p in peers):
                self.journal.decision(s,cohort,delivery_started_ms,'symbol_already_active')
                continue
            minutes=(cfg.cooldown_minutes if cfg.primary_strategy=='standard'
                     else max(cfg.cooldown_minutes,240 if s.side=='SHORT' else 180))
            if cohort=='primary':
                if not self.storage.allowed(s.symbol,s.side,minutes):
                    self.journal.decision(s,cohort,delivery_started_ms,'cooldown')
                    continue
            elif not self.journal.cooldown(cohort,s,minutes,delivery_started_ms):
                self.journal.decision(s,cohort,delivery_started_ms,'cooldown')
                continue
            if s.strategy_version=='v3' and correlated(s,peers,self.snapshots,cfg.correlation_threshold):
                self.journal.decision(s,cohort,delivery_started_ms,'correlation')
                continue
            # Fresh quote directly before each delivery; never reuse scan book.
            quote_started=time.monotonic()
            book=self.client.get('/fapi/v1/ticker/bookTicker',symbol=s.symbol)
            quote_fetch_ms=int((time.monotonic()-quote_started)*1000)
            decision=int(time.time()*1000)
            reason=prepare(s,book,decision,cfg)
            log.info("delivery_latency cohort=%s symbol=%s side=%s candle_age_ms=%d result=%s",
                     cohort,s.symbol,s.side,decision-(s.snapshot_ts+getattr(s,'timeframe_ms',60000)),reason or "ready")
            if cohort=='primary':
                research=getattr(s,'research',{}) or {}
                log.info("primary_delivery_phases symbol=%s side=%s result=%s snapshot_age_ms=%s eval_to_delivery_ms=%s quote_fetch_ms=%d quote_age_ms=%s",
                         s.symbol,s.side,reason or 'ready',
                         (research['snapshot_completed_ms']-(s.snapshot_ts+getattr(s,'timeframe_ms',60000))
                          if research.get('snapshot_completed_ms') is not None else None),
                         (decision-research['evaluation_completed_ms']
                          if research.get('evaluation_completed_ms') is not None else None),
                         quote_fetch_ms,decision-int(book['time']) if book.get('time') is not None else None)
            if reason:
                self.journal.decision(s,cohort,decision,reason)
                continue
            if not self.journal.claim(cohort,s,decision):
                self.journal.decision(s,cohort,decision,'setup_already_used')
                continue
            if cohort=='primary' and not cfg.dry_run:
                try:
                    send_telegram(cfg.telegram_token,cfg.telegram_chat_id,format_signal(s,cfg.timezone))
                    self.storage.mark_sent(s.symbol,s.side,signal=s)
                    self.journal.delivery(cohort,s,'sent')
                except Exception:
                    # Telegram cannot guarantee idempotent delivery. An uncertain
                    # attempt is never automatically resent after restart.
                    self.journal.delivery(cohort,s,'uncertain')
                    raise
                log.info('telegram_sent symbol=%s side=%s version=%s',s.symbol,s.side,s.strategy_version)
            else:
                self.journal.add_shadow(cohort,s,decision)
                if cfg.dry_run:
                    log.info('DRY_RUN cohort=%s symbol=%s side=%s',cohort,s.symbol,s.side)
            peers.append(dict(symbol=s.symbol,side=s.side))
            self.journal.decision(s,cohort,decision,'sent' if cohort=='primary' and not cfg.dry_run else 'paper_recorded')
            sent+=1
        return sent


def main():
    logging.basicConfig(level=logging.INFO,stream=sys.stdout,format='%(asctime)s %(levelname)s %(message)s')
    cfg=Config.from_env()
    cfg.validate()
    if not cfg.v2_enabled:
        from .legacy_main import main as legacy_main
        legacy_main()
        return
    stop=threading.Event()
    for sig in (system_signal.SIGTERM,system_signal.SIGINT):
        system_signal.signal(sig,lambda *_:stop.set())
    runner=Runner(cfg)
    try:
        if not runner.journal.lock():
            raise RuntimeError('another_worker_holds_lock')
        log.info('worker_started primary=%s shadow=%s continuous=%s symbols=%d',
                 cfg.primary_strategy,cfg.shadow_enabled,cfg.continuous,len(cfg.symbols))
        failures=0
        while not stop.is_set():
            started=time.time()
            try:
                runner.tick()
                failures=0
            except Exception as exc:
                failures+=1
                log.error('tick_failed kind=%s consecutive=%d',type(exc).__name__,failures)
                # Never reconnect in-place: reacquire the advisory lock on restart.
                if not cfg.continuous or failures>=3:
                    raise
            if not cfg.continuous:
                break
            # A small publication allowance after each UTC minute boundary.
            delay=max(.1,(int(started//60)+1)*60+3-time.time())
            stop.wait(delay)
    finally:
        runner.close()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        log.error('run_failed kind=%s',type(exc).__name__)
        sys.exit(1)
