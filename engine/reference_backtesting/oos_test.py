"""
PRE-REGISTERED out-of-sample test (written before running on 2013-2023 data).
Hypotheses formed on the 2023-10..2026-10 discovery sample; tested on 2013-01..2023-09.
Baseline exit (fixed 15p stop, 2R target), 1-pip round-trip cost, engine config as committed.

H0  Edge exists:            OOS baseline profit factor > 1.2.
H1  NY-open session:        PF(08-12 ET) > 1.2 AND PF(08-12 ET) > PF(all other sessions).
H2  High volatility hurts:  PF(high-ATR tercile) < 1.0 AND < PF(low+mid ATR terciles).
H3  Direction asymmetry:    PF(long in macro-uptrend) < 1.0 AND PF(short in macro-downtrend) > 1.2.
H4  Trend strength helps:   PF rises from low -> high efficiency-ratio tercile (monotonic).
A bucket verdict needs n >= 30 trades; otherwise "inconclusive". Terciles are per calendar year.
"""
import argparse, os, sys, json, numpy as np, pandas as pd
sys.dont_write_bytecode=True
HERE=os.path.dirname(os.path.abspath(__file__))
sys.path[:0]=[os.path.dirname(HERE),HERE]
from backtest_book2 import *
cfg=Book2Config(); rng=np.random.default_rng(3)

def features(d):
    c=d.Close; sma=c.rolling(2000,min_periods=2000).mean(); d["macro_up"]=c>sma
    step=c.diff().abs(); d["er480"]=(c-c.shift(480)).abs()/step.rolling(480).sum()
    tr=pd.concat([d.High-d.Low,(d.High-c.shift()).abs(),(d.Low-c.shift()).abs()],axis=1).max(axis=1)
    d["atr96"]=tr.rolling(96).mean()/0.0001
    for col,b in (("er480","er_b"),("atr96","atr_b")):
        def cut(s):
            q1,q2=s.quantile([1/3,2/3]); return pd.cut(s,[-np.inf,q1,q2,np.inf],labels=["low","mid","high"])
        d[b]=d.groupby(d.index.year,group_keys=False)[col].apply(cut)
    h=d.index.hour; d["session"]=np.select([(h>=19)|(h<2),(h>=2)&(h<8),(h>=8)&(h<12)],["Asia","London","NY-open"],"NY-pm")
    return d

def label(trades,d):
    t=pd.DataFrame(trades); t["entry_time"]=pd.to_datetime(t.entry_time,utc=True).dt.tz_convert("America/New_York")
    f=d[["macro_up","er_b","atr_b","session"]].reindex(t.entry_time).reset_index(drop=True)
    t=pd.concat([t.reset_index(drop=True),f],axis=1)
    t["dir_macro"]=t.direction+"/"+np.where(t.macro_up.astype(bool),"macro-up","macro-down"); return t

def pf(g):
    w=g[g.pnl>0].pnl.sum(); l=abs(g[g.pnl<0].pnl.sum()); return (w/l) if l else np.inf
def st(g):
    if len(g)==0: return dict(n=0,win=0,PF=np.nan,avgR=np.nan,lo=np.nan,hi=np.nan)
    R=g.r_multiple.values; bs=[rng.choice(R,len(R)).mean() for _ in range(2000)]
    return dict(n=len(g),win=round((g.pnl>0).mean()*100),PF=round(pf(g),2),avgR=round(R.mean(),2),lo=round(np.percentile(bs,5),2),hi=round(np.percentile(bs,95),2))
def row(name,g):
    s=st(g); return f"{name:<26}{s['n']:>5}{s['win']:>6}{s['PF']:>7}{s['avgR']:>7}   [{s['lo']},{s['hi']}]"

def report(label_,t):
    print(f"\n================ {label_} ================")
    print(f"{'bucket':<26}{'n':>5}{'win%':>6}{'PF':>7}{'avgR':>7}   90% CI avgR")
    print(row("ALL",t))
    for col,keys in (("session",["NY-open","London","Asia","NY-pm"]),("atr_b",["low","mid","high"]),("er_b",["low","mid","high"]),("dir_macro",["long/macro-up","long/macro-down","short/macro-up","short/macro-down"])):
        for k in keys: print(row(f"{col}={k}",t[t[col]==k]))
    print("by year PF/n:", {y:f"{round(pf(g),2)}/{len(g)}" for y,g in t.groupby(t.entry_time.dt.year)})

def verdicts(t):
    ok=lambda g:len(g)>=30
    out={}
    out["H0"]=("SUPPORTED" if pf(t)>1.2 else "REJECTED", f"PF {pf(t):.2f}, n={len(t)}")
    a=t[t.session=="NY-open"]; b=t[t.session!="NY-open"]
    out["H1"]=(("SUPPORTED" if (pf(a)>1.2 and pf(a)>pf(b)) else "REJECTED") if ok(a) else "INCONCLUSIVE", f"NY-open PF {pf(a):.2f} (n={len(a)}) vs others {pf(b):.2f} (n={len(b)})")
    a=t[t.atr_b=="high"]; b=t[t.atr_b.isin(["low","mid"])]
    out["H2"]=(("SUPPORTED" if (pf(a)<1.0 and pf(a)<pf(b)) else "REJECTED") if ok(a) else "INCONCLUSIVE", f"high-vol PF {pf(a):.2f} (n={len(a)}) vs low+mid {pf(b):.2f} (n={len(b)})")
    a=t[t.dir_macro=="long/macro-up"]; b=t[t.dir_macro=="short/macro-down"]
    out["H3"]=(("SUPPORTED" if (pf(a)<1.0 and pf(b)>1.2) else "REJECTED") if (ok(a) and ok(b)) else "INCONCLUSIVE", f"long-in-uptrend PF {pf(a):.2f} (n={len(a)}); short-in-downtrend PF {pf(b):.2f} (n={len(b)})")
    p=[pf(t[t.er_b==k]) for k in ("low","mid","high")]
    out["H4"]=(("SUPPORTED" if p[0]<p[1]<p[2] else "REJECTED"), f"ER terciles PF low/mid/high {p[0]:.2f}/{p[1]:.2f}/{p[2]:.2f}")
    return out

if __name__=="__main__":
    ap=argparse.ArgumentParser(description="Pre-registered out-of-sample test (see module docstring).")
    ap.add_argument("--old-csv",default="eur_usd_15m_2013-01_to_2023-09.csv",help="out-of-sample data")
    ap.add_argument("--new-csv",default="eur_usd_15m_2023-10_to_2026-10.csv",help="discovery-sample data")
    ap.add_argument("--compare-json",default="compare_full.json",help="output of backtest_book2.py --compare on the discovery sample")
    ap.add_argument("--out-dir",default=".")
    args=ap.parse_args(); OLD=args.old_csv
    # discovery sample, re-labelled with the same per-year-tercile method for a like-for-like comparison
    new=pd.read_csv(args.new_csv); new["time"]=pd.to_datetime(new.time,utc=True).dt.tz_convert("America/New_York"); new=features(new.set_index("time"))
    tn=label(json.load(open(args.compare_json))["baseline"]["trades"],new)
    report("DISCOVERY 2023-10..2026-10 (per-year terciles)",tn)

    old=load_data(OLD); print("\nOOS data:",len(old),"candles",old.index.min(),"->",old.index.max())
    import time; t0=time.time(); ind=compute_indicators_book2(old,cfg); print("indicators",round(time.time()-t0),"s")
    trades,eq,rs=run_backtest_book2(old,cfg,10000.0,ExitRules(),1.0,df_ind=ind); print("backtest done",round(time.time()-t0),"s | review trigger fired:",rs["triggered"],rs.get("triggered_at"))
    json.dump(trades,open(os.path.join(args.out_dir,"oos_trades.json"),"w"),default=str)
    to=label(trades,features(old.copy()))
    report("OUT-OF-SAMPLE 2013-01..2023-09",to)
    m=compute_metrics(trades,10000.0,eq); print("\nOOS headline:",{k:m[k] for k in ("total_trades","win_rate","profit_factor","total_return_pct","max_drawdown_pct")})
    print("\n######## PRE-REGISTERED VERDICTS (OOS) ########")
    for h,(v,detail) in verdicts(to).items(): print(f"{h}: {v:<13} {detail}")
    print("\nOOS cost sensitivity (baseline):")
    for cst in (0,1,2):
        tr,e2,_=run_backtest_book2(old,cfg,10000.0,ExitRules(),cst,df_ind=ind); mm=compute_metrics(tr,10000.0,e2); print(f" cost {cst}: n {mm['total_trades']} PF {mm['profit_factor']} ret% {mm['total_return_pct']} DD% {mm['max_drawdown_pct']}")
