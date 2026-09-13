from __future__ import annotations
import argparse, asyncio, hashlib, json, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from models.market_event import MarketEventType
from replay.dataset import HistoricalDataset, HistoricalMarketEvent, HistoricalDatasetManifest
from replay.runner import HistoricalPaperReplayRunner, ReplayConfig

API = "https://data-api.binance.vision/api/v3/klines"
SYMBOLS = ("BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","LTCUSDT","BCHUSDT","UNIUSDT","ATOMUSDT","NEARUSDT","APTUSDT","SUIUSDT","INJUSDT","PEPEUSDT","WIFUSDT")
TF_MIN = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}
MOVE_THRESHOLDS=(10,20,30,50,100,150,200)
WINDOW_STEPS={"5m":1,"15m":3,"30m":6,"1h":12,"4h":48,"24h":288}
END=datetime(2026,8,25,tzinfo=timezone.utc); START=END-timedelta(days=7); WARMUP=START-timedelta(days=260)


def fetch(symbol,start,end):
    out=[]; cur=int(start.timestamp()*1000); stop=int(end.timestamp()*1000)
    while cur<stop:
        q=urlencode({"symbol":symbol,"interval":"5m","startTime":cur,"endTime":stop,"limit":1000})
        req=Request(f"{API}?{q}",headers={"User-Agent":"ORION-real-historical-replay/1.0"})
        for attempt in range(5):
            try:
                with urlopen(req,timeout=30) as r: rows=json.loads(r.read().decode())
                break
            except Exception:
                if attempt==4: raise
                time.sleep(1.5*(attempt+1))
        if not rows: break
        out.extend(rows); nxt=int(rows[-1][0])+300000
        if nxt<=cur: raise RuntimeError(f"pagination stalled: {symbol}")
        cur=nxt
        if len(rows)<1000: break
    return [r for r in out if int(r[0])>=int(start.timestamp()*1000) and int(r[0])<stop]


def aggregate(rows,minutes):
    bucket=minutes*60*1000; groups=defaultdict(list)
    for r in rows: groups[(int(r[0])//bucket)*bucket].append(r)
    result=[]; needed=minutes//5
    for b,g in sorted(groups.items()):
        g.sort(key=lambda r:int(r[0]))
        if len(g)!=needed: continue
        result.append([b,g[0][1],max(float(r[2]) for r in g),min(float(r[3]) for r in g),g[-1][4],sum(float(r[5]) for r in g),b+bucket-1,"0",sum(int(r[8]) for r in g),"0","0","0"])
    return result


def acquire(root):
    acquired=datetime.now(timezone.utc).isoformat(); raw={}; completeness={}
    for s in SYMBOLS:
        rows=fetch(s,WARMUP,END); expected=int((END-WARMUP).total_seconds()/300); actual=len(rows)
        ok=actual>=expected-2; completeness[s]={"expected_5m":expected,"actual_5m":actual,"complete":ok}
        if not ok: raise RuntimeError(f"incomplete {s}: {actual}/{expected}")
        raw[s]=rows
    candles={}
    for s,rows in raw.items():
        for tf,m in TF_MIN.items(): candles[(s,tf)]=tuple(tuple(r) for r in (rows if m==5 else aggregate(rows,m)))
    lo=int(START.timestamp()*1000); hi=int(END.timestamp()*1000); events=[]
    for s,rows in raw.items():
        for r in rows:
            if lo<=int(r[6])<hi:
                ts=datetime.fromtimestamp(int(r[6])/1000,tz=timezone.utc)
                events.append(HistoricalMarketEvent(ts,s,MarketEventType.CANDLE_CLOSE,{"timeframe":"5m","open_time":datetime.fromtimestamp(int(r[0])/1000,tz=timezone.utc).isoformat(),"close_time":ts.isoformat(),"open":float(r[1]),"high":float(r[2]),"low":float(r[3]),"close":float(r[4]),"volume":float(r[5]),"is_closed":True},f"{s}:5m:{int(r[0])}"))
    events=tuple(sorted(events,key=lambda e:(e.timestamp,e.symbol,e.event_type.value,e.event_id)))
    metadata=((START,{"exchange_info":{"symbols":[{"symbol":s,"status":"TRADING","baseAsset":s[:-4],"quoteAsset":"USDT"} for s in SYMBOLS]},"ticker_24h":[],"book_ticker":[],"metadata_policy":"fixed established universe; no current/future universe enumeration"}),)
    manifest=HistoricalDatasetManifest(f"{START.isoformat()}/{END.isoformat()}","Binance Spot public market data via data-api.binance.vision",SYMBOLS,("candle_close",),tuple(TF_MIN),"UTC milliseconds; event timestamp=candle close","timestamp,symbol,event_type,stable source id","binance-spot-5m-derived-ohlcv-v1","pending")
    HistoricalDataset(manifest,events,metadata,candles).write_directory(root)
    m=json.loads((root/"manifest.json").read_text()); m.update({"acquisition_timestamp":acquired,"coverage_start":WARMUP.isoformat(),"coverage_end":END.isoformat(),"campaign_start":START.isoformat(),"campaign_end":END.isoformat(),"completeness_status":"PASS","completeness":completeness,"universe_policy":"fixed 20-symbol historical universe; symbols established before campaign start","derived_data":"15m/30m/1h/4h/1d OHLCV aggregated only from preloaded 5m data","acquisition_method":"public unauthenticated Binance market-data endpoint; no credentials","sha256_scope":"canonical dataset digest in integrity_sha256"}); (root/"manifest.json").write_text(json.dumps(m,indent=2,sort_keys=True)+"\n")


def moves(root):
    d=HistoricalDataset.from_directory(root); result=[]; lo=int(START.timestamp()*1000); hi=int(END.timestamp()*1000)
    for s in d.manifest.symbols:
        rows=d.candles[(s,"5m")]; closes=[(int(r[6]),float(r[4])) for r in rows]; index={t:i for i,(t,_) in enumerate(closes)}
        for w,n in WINDOW_STEPS.items():
            for threshold in MOVE_THRESHOLDS:
                active=False
                for t,p in closes:
                    if not lo<=t<hi or t not in index or index[t]<n: continue
                    base=closes[index[t]-n][1]; pct=(p/base-1)*100
                    if pct>=threshold and not active: result.append({"symbol":s,"timestamp":datetime.fromtimestamp(t/1000,tz=timezone.utc).isoformat(),"window":w,"threshold_pct":threshold,"movement_pct":pct,"base_price":base,"price":p}); active=True
                    elif pct<threshold: active=False
    return result


def run(root,out):
    d=HistoricalDataset.from_directory(root); cfg=ReplayConfig(campaign="7D",acceleration_factor=600,end_policy="CLOSE_AT_END",active_top_n=10,broad_pool_top_n=20); t=time.monotonic()
    runner=HistoricalPaperReplayRunner.build(d,out,replay_config=cfg,starting_capital=200); report=asyncio.run(runner.run_replay(d,replay_config=cfg)); wall=time.monotonic()-t
    report.update({"wall_clock_duration_seconds":wall,"simulation_duration_seconds":(d.end-d.start).total_seconds(),"speedup_ratio":(d.end-d.start).total_seconds()/wall,"dataset_manifest":json.loads((root/"manifest.json").read_text()),"large_moves":moves(root),"large_move_count":len(moves(root)),"safety":{"paper_only":True,"credentials_used":False,"live_orders":False,"production_execution":False}})
    (out/"campaign_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,default=str)+"\n"); return report


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",type=Path,default=Path("real_replay_7d")); a=p.parse_args(); root=a.output/"dataset"; out=a.output/"run"; root.mkdir(parents=True,exist_ok=True); out.mkdir(parents=True,exist_ok=True); acquire(root); r=run(root,out); print(json.dumps({"campaign":"7D","large_move_count":r["large_move_count"],"report":str(out/"campaign_report.json")}))
if __name__=="__main__": main()
