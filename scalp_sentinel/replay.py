"""Chronological replay of observed frames; never synthesizes missing OI history.

python -m scalp_sentinel.replay --start 2026-11-01T00:00:00Z --end ...
Use a later, untouched date window for evaluation. No automatic tuning/promotion.
"""
import argparse
import gzip
import json
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime
from itertools import groupby
from .config import Config
from .engine import evaluate
from .execution import prepare
from .journal import paper_trade, funding
from .research import candidate_v3, correlated, regime
from .tracker import ACTIVE, advance_trade
from .performance import summarize


def validate_frame(snapshot, observed_ms):
    if snapshot.get("asof_ms",observed_ms)>observed_ms:
        raise ValueError("Future observation")
    for tf in ("1m","5m","15m","1h","4h"):
        if any(int(b[6])>=observed_ms for b in snapshot[tf]):
            raise ValueError("Future/open candle in replay frame")
    for key,lag in (("taker",300000),("oi",0),("oi5",0)):
        if any(int(b["timestamp"])+lag>observed_ms for b in snapshot.get(key,[])):
            raise ValueError("Future flow data in replay frame")


def replay(frames,cfg,version="v3"):
    snapshots={}
    trades=[]
    used=set()
    last_sent={}
    for now,group in groupby(frames,key=lambda f:f[0]):
        current=[]
        for observed,symbol,snapshot in group:
            validate_frame(snapshot,observed)
            snapshots[symbol]=snapshot
            current.append(symbol)
            for t in trades:
                if t['symbol']!=symbol or t['status'] not in ACTIVE:
                    continue
                bars=[b for b in snapshot['1m'] if int(b[0])>t['last_checked_ms']]
                if bars and int(bars[0][0])!=t['last_checked_ms']+60000:
                    t['status']='DATA_GAP'
                    t['finished_at_ms']=now
                    continue
                if bars:
                    updated=advance_trade(t,bars)
                    t.update(updated)
        btc=snapshots.get('BTCUSDT')
        if btc and now-btc.get('asof_ms',0)>90000:
            btc=None
        eligible=[]
        for symbol in current:
            snap=snapshots[symbol]
            if symbol not in cfg.symbols:
                continue
            if version=='v3':
                signals=[candidate_v3(symbol,side,snap,snap['tick_size'],cfg,btc) for side in ('LONG','SHORT')]
            else:
                signals=evaluate(symbol,snap,snap['tick_size'],cfg)
            for s in signals:
                if s is not None and s.eligible:
                    s.strategy_version=version
                    if version!='v3':s.research=regime(snap)
                    eligible.append(s)
        for s in sorted(eligible,key=lambda s:(s.score,s.rvol),reverse=True):
            peers=[t for t in trades if t['status'] in ACTIVE]
            recent=sum(t['sent_at_ms']>now-900000 for t in trades)
            cooldown=max(cfg.cooldown_minutes,240 if s.side=='SHORT' else 180)*60000
            if len(peers)>=cfg.max_open_signals or recent>=cfg.max_signals_per_run:
                break
            if s.setup_id in used or any(t['symbol']==s.symbol for t in peers):
                continue
            if now-last_sent.get((s.symbol,s.side),-10**15)<cooldown:
                continue
            if version=='v3' and correlated(s,peers,snapshots,cfg.correlation_threshold):
                continue
            if prepare(s,snapshots[s.symbol]['book'],now,cfg):
                continue
            used.add(s.setup_id)
            last_sent[(s.symbol,s.side)]=now
            trades.append(paper_trade(s,now))
    return trades


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True)
    parser.add_argument('--version',choices=('v2.1','v3'),default='v3')
    parser.add_argument('--settle-funding',action='store_true')
    args=parser.parse_args()
    start=int(datetime.fromisoformat(args.start.replace('Z','+00:00')).timestamp()*1000)
    end=int(datetime.fromisoformat(args.end.replace('Z','+00:00')).timestamp()*1000)
    if end<=start:parser.error('end must be after start')
    cfg=replace(Config.from_env(),dry_run=True,v2_enabled=True)
    cfg.validate()
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        with conn.cursor(name='replay_frames') as cursor:
            cursor.execute('SELECT observed_ms,symbol,payload FROM scalp_replay_frames WHERE observed_ms>=%s AND observed_ms<%s ORDER BY observed_ms,symbol',(start,end))
            frames=((t,s,json.loads(gzip.decompress(bytes(p)))) for t,s,p in cursor)
            trades=replay(frames,cfg,args.version)
    if args.settle_funding:
        from .binance_client import BinanceClient
        client=BinanceClient(cfg.base_url)
        for t in trades:
            if t['status'] not in ACTIVE and t['status']!='DATA_GAP':
                funding(t,client,end)
    print(json.dumps(dict(version=args.version,window_start=args.start,window_end=args.end,
        statuses=dict(Counter(t['status'] for t in trades)),groups=summarize(trades),
        limitations=['Observed-frame paper replay; notification latency and actual fills not reproduced',
                     'Unsettled funding excluded from complete net results; data gaps remain unresolved',
                     'Archive retention limits available windows; no automatic promotion']),indent=2))


if __name__=='__main__':main()
