"""Point-in-time paper replay for the frozen swing hypothesis.

Historical order books are unavailable: quote spread and 5-minute execution
latency are explicit assumptions. OI is deliberately absent from this baseline.
"""
import argparse
import json
from bisect import bisect_left
from datetime import datetime, timezone

from .binance_client import BinanceClient
from .journal import paper_trade
from .performance import settle_funding, summarize
from .research import correlation
from .swing_engine import DAY, FOUR_HOURS, HOUR, evaluate_swing
from .swing_execution import MAX_HOLD_MS, prepare_swing
from .tracker import ACTIVE, advance_trade


def fetch_bars(client,symbol,interval,period,start_ms,end_ms):
    rows=[]
    cursor=start_ms
    while cursor<end_ms:
        page=client.get("/fapi/v1/klines",symbol=symbol,interval=interval,
                        startTime=cursor,endTime=min(end_ms-1,cursor+1000*period-1),limit=1000)
        page=[r for r in page if cursor<=int(r[0])<end_ms]
        if not page:
            break
        if any(int(b[0])-int(a[0])!=period for a,b in zip(page,page[1:])):
            raise ValueError(f"historical gap {symbol} {interval}")
        rows.extend(page)
        next_cursor=int(page[-1][0])+period
        if next_cursor<=cursor:
            raise ValueError("nonadvancing history")
        cursor=next_cursor
    if any(int(b[0])-int(a[0])!=period for a,b in zip(rows,rows[1:])):
        raise ValueError(f"historical page gap {symbol} {interval}")
    return rows


def asof(rows,timestamp,period):
    """Only bars already closed at timestamp, never an open candle."""
    opens=[int(r[0]) for r in rows]
    return rows[:bisect_left(opens,timestamp-period+1)]


def aggregate(trades):
    all_rows=[dict(t,symbol="ALL",side="ALL",
                   journal_json=dict(t.get("journal_json") or {},regime="ALL")) for t in trades]
    return summarize(all_rows)


def replay(client,symbols,start_ms,end_ms,spread_bps=5.0):
    """Chronological replay with a time holdout and conservative 1m fills."""
    universe=tuple(dict.fromkeys(("BTCUSDT",*symbols)))
    info=client.symbol_info()
    data={}
    for sym in universe:
        if sym not in info or info[sym].get("contractType")!="PERPETUAL":
            continue
        data[sym]={
            "1d":fetch_bars(client,sym,"1d",DAY,start_ms-410*DAY,end_ms),
            "4h":fetch_bars(client,sym,"4h",FOUR_HOURS,start_ms-20*DAY,end_ms),
            "1h":fetch_bars(client,sym,"1h",HOUR,start_ms-3*DAY,end_ms),
        }
    if "BTCUSDT" not in data:
        raise ValueError("BTC context unavailable")
    trades=[]
    rejected={}
    last_by_symbol={}
    gap_count=0
    for t in range((start_ms+FOUR_HOURS-1)//FOUR_HOURS*FOUR_HOURS,end_ms,FOUR_HOURS):
        snapshots={}
        btc_daily=asof(data["BTCUSDT"]["1d"],t,DAY)
        for sym,history in data.items():
            day=asof(history["1d"],t,DAY)
            four=asof(history["4h"],t,FOUR_HOURS)
            hour=asof(history["1h"],t,HOUR)
            if not (day and four and hour) or int(four[-1][0])+FOUR_HOURS!=t:
                continue
            close=float(hour[-1][4])
            snapshots[sym]={"1d":day,"4h":four,"1h":hour,
                "quote_volume":float(day[-1][7]),
                "book":{"bidPrice":close*(1-spread_bps/20000),
                        "askPrice":close*(1+spread_bps/20000)}}
        candidates=[]
        for sym in symbols:
            if sym not in snapshots:
                continue
            tick=next((f["tickSize"] for f in info[sym]["filters"]
                       if f["filterType"]=="PRICE_FILTER"),None)
            if not tick:
                continue
            candidates.extend(s for s in evaluate_swing(sym,snapshots[sym],btc_daily,tick) if s.eligible)
        active=[p for p in trades if p.get("finished_at_ms") is None or p["finished_at_ms"]>t]
        recent=[p for p in trades if p["sent_at_ms"]>t-24*HOUR]
        for s in sorted(candidates,key=lambda item:item.priority,reverse=True):
            reason=None
            if recent:
                reason="daily_cap"
            elif len(active)>=2 or any(p["symbol"]==s.symbol for p in active):
                reason="portfolio_cap"
            elif t-last_by_symbol.get((s.symbol,s.side),-10**20)<7*DAY:
                reason="cooldown"
            else:
                a={int(b[0]):float(b[4])/float(prev[4])-1 for prev,b in zip(snapshots[s.symbol]["4h"][-61:-1],snapshots[s.symbol]["4h"][-60:])}
                for p in active:
                    if p["symbol"] not in snapshots:
                        reason="unknown_exposure"
                        break
                    peer=snapshots[p["symbol"]]["4h"]
                    b={int(bar[0]):float(bar[4])/float(prev[4])-1 for prev,bar in zip(peer[-61:-1],peer[-60:])}
                    corr=correlation(a,b)
                    if corr is None or corr*(1 if p["side"]==s.side else -1)>=.75:
                        reason="correlation"
                        break
            decision=t+5*60000+3000
            if reason is None:
                # The first observed price after the assumed five-minute delay
                # is a 1m open, never a future bar's close or best intrabar fill.
                quote=fetch_bars(client,s.symbol,"1m",60000,t+5*60000,t+6*60000)
                if len(quote)!=1:
                    reason="missing_decision_quote"
                else:
                    mid=float(quote[0][1])
                    book={"bidPrice":mid*(1-spread_bps/20000),
                          "askPrice":mid*(1+spread_bps/20000),"time":decision}
                    reason=prepare_swing(s,book,decision)
            if reason:
                rejected[reason]=rejected.get(reason,0)+1
                continue
            trade=paper_trade(s,decision)
            trade.update(cohort="swing-shadow",strategy_version="swing-shadow")
            start=decision//60000*60000+60000
            end=min(end_ms,decision+MAX_HOLD_MS+2*HOUR)
            bars=fetch_bars(client,s.symbol,"1m",60000,start,end)
            if any(int(b[0])!=start+i*60000 for i,b in enumerate(bars)) or (bars and int(bars[-1][0])+60000<end-60000):
                trade["status"]="DATA_GAP"
                trade["finished_at_ms"]=decision+MAX_HOLD_MS
                gap_count+=1
            elif bars:
                trade=advance_trade(trade,bars)
            else:
                trade["status"]="DATA_GAP"
                trade["finished_at_ms"]=decision+MAX_HOLD_MS
                gap_count+=1
            if trade["status"] not in ACTIVE and trade["status"] not in ("DATA_GAP","NO_ENTRY","INVALIDATED"):
                try:
                    events=client.get("/fapi/v1/fundingRate",symbol=s.symbol,
                        startTime=trade["filled_at_ms"] or decision,
                        endTime=trade["finished_at_ms"]+60000,limit=1000)
                    settle_funding(trade,events)
                except Exception:
                    rejected["funding_unavailable"]=rejected.get("funding_unavailable",0)+1
            trades.append(trade)
            recent.append(trade)
            active.append(trade)
            last_by_symbol[(s.symbol,s.side)]=decision
    split=start_ms+(end_ms-start_ms)*2//3
    development_end=split-MAX_HOLD_MS
    return dict(strategy="swing-trend-v1",start_ms=start_ms,end_ms=end_ms,
                development_end_ms=development_end,holdout_start_ms=split,
                development=aggregate([p for p in trades if p["sent_at_ms"]<development_end]),
                holdout=aggregate([p for p in trades if p["sent_at_ms"]>=split]),
                statuses={status:sum(p["status"]==status for p in trades)
                          for status in sorted({p["status"] for p in trades})},
                data_gaps=gap_count,rejected=rejected,
                assumptions=["Historical spread is assumed; order book, actual fills and liquidation are not reproduced",
                             "Decision price is the 1m open five minutes after the 4h close",
                             "OI excluded; historical endpoint does not span the full replay",
                             "Funding only complete after an observed terminal trade and successful rate query",
                             "No fitted thresholds or automatic promotion"])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",required=True,help="UTC ISO date")
    parser.add_argument("--end",required=True,help="UTC ISO date")
    parser.add_argument("--symbols",default="BTCUSDT,ETHUSDT,SOLUSDT")
    args=parser.parse_args()
    dates=[datetime.fromisoformat(v.replace("Z","+00:00")) for v in (args.start,args.end)]
    dates=[v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v for v in dates]
    start,end=(int(v.timestamp()*1000) for v in dates)
    if end<=start or end-start<300*DAY:
        parser.error("a replay window of at least 300 days is required")
    symbols=tuple(x.strip().upper() for x in args.symbols.split(",") if x.strip())
    client=BinanceClient("https://fapi.binance.com")
    print(json.dumps(replay(client,symbols,start,end),ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
