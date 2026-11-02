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
from .research import candidate_v3, correlated, regime
from .confluence import detect_confluences, format_confluence_alert
from .price_zones import (detect_price_zones, nearby_zone_alert, should_watch_zones,
                          format_price_zone_alert, format_price_zone_digest, current_price)
from .storage import Cooldowns
from .tracker import track_signals

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
        self.level_watch=set()
        self.level_zones={}
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
        self.journal.alive()  # lost database session also loses the worker lock
        if now_ms is None:
            now_ms=int(time.time()*1000)
        full=now_ms//300000 != self.full_bucket
        if full:
            self.exchange=self.client.symbol_info()
            remote=int(self.client.get('/fapi/v1/time')['serverTime'])
            if abs(remote-int(time.time()*1000))>30000:
                raise RuntimeError('exchange_clock_skew')
        symbols=[s for s in cfg.symbols if s in self.exchange]
        if not symbols:
            raise RuntimeError('No supported configured symbols')
        wanted=set(symbols) if full else (self.watch | self.pending | self.level_watch) & set(symbols)
        wanted.add('BTCUSDT')
        # Open shadow exposures must have current correlation context too.
        for peer in self.journal.active('v3-shadow')+self.journal.active('primary'):
            wanted.add(peer['symbol'])
        candidates={}
        confluence_alerts=[]
        level_alerts=[]
        next_level_watch=set()
        errors=[]
        pending=[]
        data_pending_counts={}
        gate_pass={}
        gate_fail={}
        prefilter_counts={}
        evaluated_sides=0
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(self.client.snapshot,s,now_ms):s for s in wanted if s in self.exchange}
            for future in as_completed(jobs):
                sym=jobs[future]
                try:
                    snap=future.result()
                    tick=next(f['tickSize'] for f in self.exchange[sym]['filters'] if f['filterType']=='PRICE_FILTER')
                    snap['tick_size']=tick
                    self.snapshots[sym]=snap
                    if full and sym in symbols:
                        try:
                            confluence_alerts.extend(detect_confluences(sym,snap,tick))
                        except Exception as exc:
                            log.warning("confluence_eval_failed symbol=%s kind=%s",sym,type(exc).__name__)
                        try:
                            self.level_zones[sym]=detect_price_zones(sym,snap,tick)
                        except Exception as exc:
                            self.level_zones[sym]=[]
                            log.warning("price_zone_eval_failed symbol=%s kind=%s",sym,type(exc).__name__)
                    zones=self.level_zones.get(sym,[])
                    if zones:
                        self.journal.observe_price_zones(sym,zones,current_price(snap),now_ms)
                        alert=nearby_zone_alert(sym,snap,zones,tick)
                        if alert:
                            level_alerts.append(alert)
                        if should_watch_zones(snap,zones):
                            next_level_watch.add(sym)
                    if snap.get("oi5_pending_reason"):
                        data_pending_counts["oi5"]=data_pending_counts.get("oi5",0)+1
                        log.info("shadow_data_pending symbol=%s dataset=oi5 reason=%s",
                                 sym,snap["oi5_pending_reason"])
                    if sym in symbols:
                        evaluated=evaluate(sym,snap,tick,cfg)
                        candidates[sym]=evaluated
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
                except DataPending as exc:
                    self.snapshots.pop(sym,None)
                    pending.append(sym)
                    data_pending_counts[exc.dataset]=data_pending_counts.get(exc.dataset,0)+1
                    log.info('data_pending symbol=%s reason=%s',sym,str(exc))
                except Exception as exc:
                    self.snapshots.pop(sym,None)
                    errors.append(sym)
                    log.warning('snapshot_failed symbol=%s kind=%s reason=%s',sym,type(exc).__name__,
                                str(exc)[:160] if isinstance(exc,BinanceError) else 'unavailable')
        if full:
            self.level_watch=next_level_watch
            self.watch={sym for sym,signals in candidates.items()
                        if any(s.checks['15m trend'] and s.checks['5m structure'] for s in signals)}
            self.full_bucket=now_ms//300000
            self.pending=set(pending+errors)
        else:
            self.level_watch.difference_update(wanted)
            self.level_watch.update(next_level_watch)
            self.pending.difference_update(wanted)
            self.pending.update(pending+errors)
            for sym,signals in candidates.items():
                if any(s.checks['15m trend'] and s.checks['5m structure'] for s in signals):
                    self.watch.add(sym)
                else:
                    self.watch.discard(sym)
        primary=[]
        shadow=[]
        control=[]
        btc=self.snapshots.get('BTCUSDT')
        if btc and btc['asof_ms']!=now_ms:
            btc=None
        for sym,signals in candidates.items():
            snap=self.snapshots[sym]
            research=[]
            for s in signals:
                s.strategy_version='v2.1'
                s.research=regime(snap)
                s.research.update({k:snap.get(k) for k in ('flow_source','oi_age_ms','oi5_age_ms')})
                if (cfg.shadow_enabled or cfg.primary_strategy=='v3') and "oi5" in snap:
                    v3=candidate_v3(sym,s.side,snap,snap['tick_size'],cfg,btc)
                    if v3:
                        research.append(v3)
                        if v3.eligible:
                            (primary if cfg.primary_strategy=='v3' else shadow).append(v3)
                elif cfg.shadow_enabled or cfg.primary_strategy=='v3':
                    log.info("shadow_candidate_skipped symbol=%s side=%s dataset=oi5",
                             sym,s.side)
                if s.eligible:
                    if cfg.primary_strategy=='v2.1':
                        primary.append(s)
                    if cfg.shadow_enabled and now_ms//60000 % 15==0:
                        control.append(copy.deepcopy(s))
            self.journal.archive(sym,snap,now_ms,signals+research)
        # Archive BTC even if it is excluded from the user watchlist.
        if btc and 'BTCUSDT' not in candidates:
            self.journal.archive('BTCUSDT',btc,now_ms,[])
        if errors and len(errors)>=max(2,len(wanted)//2):
            raise RuntimeError('too_many_snapshot_failures')
        sent=self.deliver(primary,'primary',now_ms)
        shadow_count=self.deliver(shadow,'v3-shadow',now_ms) if cfg.shadow_enabled else 0
        if cfg.shadow_enabled:
            self.deliver(control,'v2-control-15m',now_ms)
        confluence_sent=self.deliver_confluences(confluence_alerts,now_ms)
        level_sent=self.deliver_price_zones(level_alerts,now_ms)
        log.info('scan_finished mode=%s full=%s scanned=%d watching=%d eligible=%d delivered=%d confluence=%d level_zones=%d shadow=%d failed=%d pending=%d',
                 cfg.primary_strategy,full,len(candidates),len(self.watch),len(primary),sent,confluence_sent,level_sent,shadow_count,len(errors),len(pending))
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

    def deliver_confluences(self,alerts,now_ms):
        """Send separate directional context alerts; never create paper trades."""
        sent=0
        for alert in alerts:
            if self.cfg.dry_run:
                log.info("DRY_RUN confluence symbol=%s side=%s timeframe=%s indicators=%s zone_id=%s",
                         alert["symbol"],alert["side"],alert["timeframe"],
                         ",".join(alert["indicators"]),alert["alert_id"])
                continue
            if not self.journal.claim_confluence(alert,now_ms):
                continue
            try:
                send_telegram(self.cfg.telegram_token,self.cfg.telegram_chat_id,
                              format_confluence_alert(alert,self.cfg.timezone))
            except Exception as exc:
                self.journal.confluence_delivery(alert,"uncertain")
                log.warning("confluence_delivery_uncertain symbol=%s side=%s timeframe=%s kind=%s",
                            alert["symbol"],alert["side"],alert["timeframe"],type(exc).__name__)
            else:
                self.journal.confluence_delivery(alert,"sent")
                log.info("confluence_telegram_sent symbol=%s side=%s timeframe=%s indicators=%s",
                         alert["symbol"],alert["side"],alert["timeframe"],
                         ",".join(alert["indicators"]))
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
            try:
                send_telegram(self.cfg.telegram_token,self.cfg.telegram_chat_id,
                              format_price_zone_alert(alert,self.cfg.timezone))
            except Exception as exc:
                self.journal.price_zone_delivery(alert,"uncertain")
                log.warning("price_zone_delivery_uncertain symbol=%s kind=%s",
                            alert["symbol"],type(exc).__name__)
            else:
                self.journal.price_zone_delivery(alert,"sent")
                log.info("price_zone_telegram_sent symbol=%s center=%s sources=%s",
                         alert["symbol"],alert["center"],",".join(alert["sources"]))
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
                        send_telegram(self.cfg.telegram_token,self.cfg.telegram_chat_id,
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
        if cohort=='primary' and self.journal.pending_delivery():
            log.error('delivery_uncertain_primary_paused reconciliation_required=true')
            return 0
        peers=self.journal.active(cohort)
        sent=0
        recent=self.journal.recent_count(cohort,now_ms)
        for s in sorted(signals,key=lambda s:(s.score,s.rvol),reverse=True):
            if sent+recent>=cfg.max_signals_per_run or len(peers)>=cfg.max_open_signals:
                self.journal.decision(s,cohort,now_ms,'exposure_or_rate_limit')
                continue
            if any(p['symbol']==s.symbol for p in peers):
                self.journal.decision(s,cohort,now_ms,'symbol_already_active')
                continue
            minutes=max(cfg.cooldown_minutes,240 if s.side=='SHORT' else 180)
            if cohort=='primary':
                if not self.storage.allowed(s.symbol,s.side,minutes):
                    self.journal.decision(s,cohort,now_ms,'cooldown')
                    continue
            elif not self.journal.cooldown(cohort,s,minutes,now_ms):
                self.journal.decision(s,cohort,now_ms,'cooldown')
                continue
            if s.strategy_version=='v3' and correlated(s,peers,self.snapshots,cfg.correlation_threshold):
                self.journal.decision(s,cohort,now_ms,'correlation')
                continue
            # Fresh quote directly before each delivery; never reuse scan book.
            book=self.client.get('/fapi/v1/ticker/bookTicker',symbol=s.symbol)
            decision=int(time.time()*1000)
            reason=prepare(s,book,decision,cfg)
            if reason:
                self.journal.decision(s,cohort,now_ms,reason)
                continue
            if not self.journal.claim(cohort,s,decision):
                self.journal.decision(s,cohort,now_ms,'setup_already_used')
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
            self.journal.decision(s,cohort,now_ms,'sent' if cohort=='primary' and not cfg.dry_run else 'paper_recorded')
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
