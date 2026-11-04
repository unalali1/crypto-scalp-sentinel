"""Independent four-hour paper swing scanner. No exchange orders or Telegram."""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .binance_client import BinanceClient
from .config import DEFAULT_SYMBOLS
from .journal import Journal
from .research import correlation
from .swing_engine import DAY, FOUR_HOURS, evaluate_swing
from .swing_execution import prepare_swing
from .swing_market import live_snapshot

log=logging.getLogger("sentinel.swing")
COHORT="swing-shadow"


def return_series_4h(snapshot):
    bars=snapshot["4h"][-61:]
    return {int(b[0]):float(b[4])/float(a[4])-1 for a,b in zip(bars,bars[1:])}


def portfolio_blocks(setup,peers,snapshots):
    series=return_series_4h(snapshots[setup.symbol])
    for peer in peers:
        if peer["symbol"]==setup.symbol or peer["symbol"] not in snapshots:
            return True
        corr=correlation(series,return_series_4h(snapshots[peer["symbol"]]))
        if corr is None or corr*(1 if peer["side"]==setup.side else -1)>=.75:
            return True
    return False


class SwingRunner:
    def __init__(self,client,journal,symbols):
        self.client=client
        self.journal=journal
        self.symbols=tuple(dict.fromkeys(symbols))

    def run(self,now_ms=None):
        now_ms=now_ms or int(time.time()*1000)
        # Cron fires five minutes after each UTC 4h close. Never act on an
        # older bar after downtime; the next scheduled scan can try again.
        if not 5*60000<=now_ms%FOUR_HOURS<=20*60000:
            log.info("swing_outside_decision_window")
            return 0
        remote=int(self.client.get("/fapi/v1/time")["serverTime"])
        if abs(remote-int(time.time()*1000))>30000:
            raise RuntimeError("exchange_clock_skew")
        info=self.client.symbol_info()
        configured=[s for s in self.symbols if s in info and info[s].get("contractType")=="PERPETUAL"]
        if not configured:
            raise RuntimeError("no_eligible_swing_symbols")
        peers=self.journal.active(COHORT)
        wanted=set(configured)|{p["symbol"] for p in peers}|{"BTCUSDT"}
        snapshots={}
        failures={}
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(live_snapshot,self.client,s,now_ms):s
                  for s in wanted if s in info and info[s].get("contractType")=="PERPETUAL"}
            for future in as_completed(jobs):
                sym=jobs[future]
                try:
                    snapshots[sym]=future.result()
                except Exception as exc:
                    failures[sym]=type(exc).__name__
                    log.warning("swing_data_pending symbol=%s kind=%s",sym,type(exc).__name__)
        if "BTCUSDT" not in snapshots:
            log.warning("swing_skipped missing_btc_context=true failures=%s",json.dumps(failures))
            return 0
        candidates=[]
        for sym in configured:
            if sym not in snapshots:
                continue
            tick=next((f["tickSize"] for f in info[sym]["filters"]
                       if f["filterType"]=="PRICE_FILTER"),None)
            if tick is None:
                log.warning("swing_skipped symbol=%s reason=missing_tick",sym)
                continue
            assessed=evaluate_swing(sym,snapshots[sym],snapshots["BTCUSDT"]["1d"],tick)
            for s in assessed:
                failed=[name for name,passed in s.checks.items() if not passed]
                log.info("swing_assessment symbol=%s side=%s eligible=%s failed=%s",
                         sym,s.side,s.eligible,json.dumps(failed,separators=(",",":")))
                self.journal.swing_assessment(s,now_ms,"eligible" if s.eligible else "failed_checks")
                if s.eligible:
                    candidates.append(s)
        sent=0
        for s in sorted(candidates,key=lambda item:item.priority,reverse=True):
            reason=None
            if sent or self.journal.swing_recent_count(now_ms)>=1:
                reason="daily_cap"
            elif len(peers)>=2:
                reason="portfolio_cap"
            elif portfolio_blocks(s,peers,snapshots):
                reason="correlation_or_unknown_exposure"
            elif not self.journal.cooldown(COHORT,s,7*24*60,now_ms):
                reason="cooldown"
            else:
                # The original book is only a snapshot; refresh the executable
                # side just before recording a hypothetical paper setup.
                book=self.client.get("/fapi/v1/ticker/bookTicker",symbol=s.symbol)
                reason=prepare_swing(s,book,int(time.time()*1000))
            if reason:
                self.journal.swing_assessment(s,now_ms,reason)
                log.info("swing_rejected symbol=%s side=%s reason=%s",s.symbol,s.side,reason)
                continue
            decision=int(time.time()*1000)
            if not self.journal.add_swing_shadow(s,decision):
                self.journal.swing_assessment(s,now_ms,"setup_already_recorded")
                continue
            self.journal.swing_assessment(s,now_ms,"paper_recorded")
            peers.append(dict(symbol=s.symbol,side=s.side))
            sent+=1
            log.info("swing_paper_recorded symbol=%s side=%s setup=%s",s.symbol,s.side,s.setup_id)
        log.info("swing_scan_finished assessed=%d eligible=%d recorded=%d failed_data=%d active=%d",
                 sum(sym in snapshots for sym in configured)*2,len(candidates),sent,len(failures),len(peers))
        return sent


def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    url=os.environ.get("DATABASE_URL","")
    if not url.startswith(("postgres://","postgresql://")):
        raise RuntimeError("swing_paper_requires_postgresql")
    import psycopg
    symbols=tuple(x.strip().upper() for x in os.getenv("SWING_SYMBOLS",DEFAULT_SYMBOLS).split(",") if x.strip())
    with psycopg.connect(url,connect_timeout=8) as conn:
        journal=Journal(conn)
        # Separate advisory key from the 1m scanner. Cron overlap fails closed.
        if not conn.execute("SELECT pg_try_advisory_lock(739201729)").fetchone()[0]:
            raise RuntimeError("swing_worker_overlap")
        conn.commit()
        SwingRunner(BinanceClient(os.getenv("BINANCE_BASE_URL","https://fapi.binance.com")),journal,symbols).run()


if __name__=="__main__":
    main()
