"""Point-in-time paper replay for the frozen swing hypothesis.

Historical order books are unavailable: quote spread and execution delay are
explicit assumptions. OI is deliberately absent from this baseline.
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
    if not trades:
        return []
    all_rows=[dict(t,symbol="ALL",side="ALL",
                   journal_json=dict(t.get("journal_json") or {},regime="ALL")) for t in trades]
    return summarize(trades)+summarize(all_rows)


def load_history(client,symbols,start_ms,end_ms):
    """Fetch each higher-timeframe history once for all paper scenarios."""
    universe=tuple(dict.fromkeys(("BTCUSDT",*symbols)))
    info=client.symbol_info()
    data={}
    skipped={}
    for sym in universe:
        if sym not in info or info[sym].get("contractType")!="PERPETUAL":
            skipped[sym]="unavailable_or_not_crypto_perpetual"
            continue
        try:
            data[sym]={
                "1d":fetch_bars(client,sym,"1d",DAY,start_ms-410*DAY,end_ms),
                "4h":fetch_bars(client,sym,"4h",FOUR_HOURS,start_ms-20*DAY,end_ms),
                "1h":fetch_bars(client,sym,"1h",HOUR,start_ms-3*DAY,end_ms),
            }
            data[sym]["opens"]={tf:[int(row[0]) for row in data[sym][tf]]
                                for tf in ("1d","4h","1h")}
        except (ValueError,RuntimeError) as exc:
            skipped[sym]=type(exc).__name__
    if "BTCUSDT" not in data or not data["BTCUSDT"]["1d"]:
        raise ValueError("BTC context unavailable")
    return info,data,skipped


def closed_asof(history,interval,timestamp,period):
    rows=history[interval]
    return rows[:bisect_left(history["opens"][interval],timestamp-period+1)]


def trade_record(trade,period):
    p=trade.get("journal_json") or {}
    keys=("symbol","side","sent_at_ms","entry_price","stop_price",
          "tp1_price","tp2_price","status","filled_at_ms","finished_at_ms")
    row={key:trade.get(key) for key in keys}
    row.update(period=period,setup_id=p.get("setup_id"),
               selection_reason=p.get("selection_reason"),
               net_r_low=p.get("net_r_low"),net_r_high=p.get("net_r_high"),
               gross_r_low=p.get("gross_r_low"),
               execution_cost_r=p.get("execution_cost_r"),
               funding_r_low=p.get("funding_r_low"),
               mfe_r=p.get("mfe_r"),mae_r=p.get("mae_r"),
               rvol4=p.get("rvol4"),spread_bps=p.get("spread_bps"),
               costs_complete=p.get("costs_complete"))
    return row


def paper_outcome(client,setup,decision,end_ms):
    trade=paper_trade(setup,decision)
    trade.update(cohort="swing-shadow",strategy_version=setup.strategy_version)
    start=decision//60000*60000+60000
    end=min(end_ms,decision+MAX_HOLD_MS+2*HOUR)
    bars=fetch_bars(client,setup.symbol,"1m",60000,start,end)
    if (not bars or any(int(b[0])!=start+i*60000 for i,b in enumerate(bars))
            or int(bars[-1][0])+60000<end-60000):
        trade["status"]="DATA_GAP"
        trade["finished_at_ms"]=decision+MAX_HOLD_MS
    else:
        trade=advance_trade(trade,bars)
    funding_failed=False
    if trade["status"] not in ACTIVE and trade["status"] not in ("DATA_GAP","NO_ENTRY","INVALIDATED"):
        try:
            events=client.get("/fapi/v1/fundingRate",symbol=setup.symbol,
                startTime=trade["filled_at_ms"] or decision,
                endTime=trade["finished_at_ms"]+60000,limit=1000)
            settle_funding(trade,events)
        except Exception:
            funding_failed=True
    return trade,funding_failed


def console_report(result):
    """Keep Railway's single log entry small without hiding the split result."""
    keys=("strategy","start_ms","end_ms","fetched_symbols","skipped_symbols",
          "development_end_ms","holdout_start_ms","statuses","data_gaps",
          "rejected","coverage","ranking","delay_minutes","spread_bps",
          "fee_bps","slippage_bps","counterfactual_count","assumptions")
    report={key:result[key] for key in keys}
    for period in ("development","holdout"):
        report[period]=next((row for row in result[period]
                             if row["symbol"]=="ALL" and row["side"]=="ALL"),None)
    return report


def replay(client,symbols,start_ms,end_ms,spread_bps=5.0,delay_minutes=5,
           fee_bps=5.0,slippage_bps=2.0,ranking="liquidity",
           counterfactual=False,history=None):
    """Chronological paper replay; diagnostics cannot change the selected book."""
    if ranking not in ("liquidity","rvol") or not 5<=delay_minutes<=19:
        raise ValueError("unsupported paper scenario")
    if any(x<0 for x in (spread_bps,fee_bps,slippage_bps)):
        raise ValueError("negative execution cost")
    info,data,skipped=history if history is not None else load_history(client,symbols,start_ms,end_ms)
    coverage={sym:{"first_day_ms":int(h["1d"][0][0]) if h["1d"] else None,
                   "last_day_ms":int(h["1d"][-1][0]) if h["1d"] else None,
                   "daily_bars":len(h["1d"]),"four_hour_bars":len(h["4h"]),
                   "hour_bars":len(h["1h"]),"evaluable_4h":0}
              for sym,h in data.items()}
    trades=[]
    counterfactual_trades=[]
    decisions=[]
    rejected={}
    last_by_symbol={}
    counterfactual_funding_missing=0
    for t in range((start_ms+FOUR_HOURS-1)//FOUR_HOURS*FOUR_HOURS,end_ms,FOUR_HOURS):
        snapshots={}
        btc_daily=closed_asof(data["BTCUSDT"],"1d",t,DAY)
        for sym,history in data.items():
            day=closed_asof(history,"1d",t,DAY)
            four=closed_asof(history,"4h",t,FOUR_HOURS)
            hour=closed_asof(history,"1h",t,HOUR)
            if not (day and four and hour) or int(four[-1][0])+FOUR_HOURS!=t:
                continue
            if len(day)>=400 and len(four)>=90 and len(hour)>=20 and len(btc_daily)>=400:
                coverage[sym]["evaluable_4h"]+=1
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
        rank_key=(lambda item:item.priority) if ranking=="liquidity" else (
                  lambda item:(item.rvol4,item.priority))
        for s in sorted(candidates,key=rank_key,reverse=True):
            decision=t+delay_minutes*60000+3000
            diagnostic=dict(bar_close_ms=t,symbol=s.symbol,side=s.side,
                            setup_id=s.setup_id,liquidity_priority=s.priority,
                            rvol4=s.rvol4,spread_bps=s.spread_bps)
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
            selection_reason=reason
            execution_reason=None
            if reason is None or counterfactual:
                # The next observed 1m open is a price proxy, not a guaranteed fill.
                quote=fetch_bars(client,s.symbol,"1m",60000,
                                 t+delay_minutes*60000,t+(delay_minutes+1)*60000)
                if len(quote)!=1:
                    execution_reason="missing_decision_quote"
                else:
                    mid=float(quote[0][1])
                    book={"bidPrice":mid*(1-spread_bps/20000),
                          "askPrice":mid*(1+spread_bps/20000),"time":decision}
                    execution_reason=prepare_swing(s,book,decision,fee_bps,slippage_bps)
                if reason is None:
                    reason=execution_reason
            diagnostic.update(selected=reason is None,reason=reason or "paper_selected",
                              execution_gate=execution_reason if counterfactual else None)
            if reason:
                rejected[reason]=rejected.get(reason,0)+1
                if counterfactual and selection_reason and execution_reason is None:
                    s.journal["selection_reason"]=selection_reason
                    hypothetical,unfunded=paper_outcome(client,s,decision,end_ms)
                    counterfactual_trades.append(hypothetical)
                    counterfactual_funding_missing+=int(unfunded)
                    diagnostic["counterfactual_status"]=hypothetical["status"]
                decisions.append(diagnostic)
                continue
            trade,unfunded=paper_outcome(client,s,decision,end_ms)
            if unfunded:
                rejected["funding_unavailable"]=rejected.get("funding_unavailable",0)+1
            trades.append(trade)
            recent.append(trade)
            active.append(trade)
            last_by_symbol[(s.symbol,s.side)]=decision
            decisions.append(diagnostic)
    split=start_ms+(end_ms-start_ms)*2//3
    development_end=split-MAX_HOLD_MS
    period=lambda p:("development" if p["sent_at_ms"]<development_end else
                     "holdout" if p["sent_at_ms"]>=split else "embargo")
    return dict(strategy="swing-trend-v1",start_ms=start_ms,end_ms=end_ms,
                fetched_symbols=sorted(data),skipped_symbols=skipped,
                development_end_ms=development_end,holdout_start_ms=split,
                development=aggregate([p for p in trades if p["sent_at_ms"]<development_end]),
                holdout=aggregate([p for p in trades if p["sent_at_ms"]>=split]),
                trade_ledger=[trade_record(p,period(p)) for p in trades],
                candidate_decisions=decisions,
                counterfactual_ledger=[trade_record(p,period(p)) for p in counterfactual_trades],
                counterfactual_count=len(counterfactual_trades),
                statuses={status:sum(p["status"]==status for p in trades)
                          for status in sorted({p["status"] for p in trades})},
                data_gaps=sum(p["status"]=="DATA_GAP" for p in trades),
                rejected=rejected,coverage=coverage,
                ranking=ranking,delay_minutes=delay_minutes,spread_bps=spread_bps,
                fee_bps=fee_bps,slippage_bps=slippage_bps,
                counterfactual_funding_missing=counterfactual_funding_missing,
                assumptions=["Historical spread is assumed; order book, actual fills and liquidation are not reproduced",
                             "Decision price is the later 1m open after the specified delay",
                             "OI excluded; historical endpoint does not span the full replay",
                             "Universe is the currently listed set; delisted historical symbols are unavailable",
                             "Counterfactual candidate outcomes overlap and cannot be summed as a portfolio",
                             "Funding only complete after an observed terminal trade and successful rate query",
                             "No fitted thresholds or automatic promotion"])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",required=True,help="UTC ISO date")
    parser.add_argument("--end",required=True,help="UTC ISO date")
    parser.add_argument("--symbols",default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--delay-minutes",type=int,default=5)
    parser.add_argument("--spread-bps",type=float,default=5.0)
    parser.add_argument("--fee-bps",type=float,default=5.0)
    parser.add_argument("--slippage-bps",type=float,default=2.0)
    parser.add_argument("--diagnostics",action="store_true",
                        help="Emit selected and rejected candidate ledgers")
    parser.add_argument("--stress",action="store_true",
                        help="Add two fixed adverse timing/cost scenarios")
    parser.add_argument("--compare-ranking",action="store_true",
                        help="Compare an RVOL-first paper-only ranking")
    args=parser.parse_args()
    dates=[datetime.fromisoformat(v.replace("Z","+00:00")) for v in (args.start,args.end)]
    dates=[v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v for v in dates]
    start,end=(int(v.timestamp()*1000) for v in dates)
    if end<=start or end-start<300*DAY:
        parser.error("a replay window of at least 300 days is required")
    symbols=tuple(x.strip().upper() for x in args.symbols.split(",") if x.strip())
    client=BinanceClient("https://fapi.binance.com")
    history=load_history(client,symbols,start,end)
    scenarios=[("baseline",dict(delay_minutes=args.delay_minutes,
                                spread_bps=args.spread_bps,fee_bps=args.fee_bps,
                                slippage_bps=args.slippage_bps,ranking="liquidity"))]
    if args.stress:
        scenarios.extend((
            ("late_10m",dict(delay_minutes=10,spread_bps=7.5,
                             fee_bps=5.0,slippage_bps=4.0,ranking="liquidity")),
            ("adverse_15m",dict(delay_minutes=15,spread_bps=7.5,
                                fee_bps=7.0,slippage_bps=5.0,ranking="liquidity"))))
    if args.compare_ranking:
        scenarios.append(("rvol_first",dict(scenarios[0][1],ranking="rvol")))
    for name,settings in scenarios:
        result=replay(client,symbols,start,end,history=history,
                      counterfactual=args.diagnostics and name=="baseline",**settings)
        emit(dict(kind="swing_replay_summary",scenario=name,**console_report(result)))
        if args.diagnostics and name=="baseline":
            for key,kind in (("trade_ledger","swing_trade"),
                             ("candidate_decisions","swing_candidate"),
                             ("counterfactual_ledger","swing_counterfactual")):
                for row in result[key]:
                    emit(dict(kind=kind,scenario=name,**row))


def emit(row):
    print(json.dumps(row,ensure_ascii=False,separators=(",",":")),flush=True)


if __name__=="__main__":
    main()
