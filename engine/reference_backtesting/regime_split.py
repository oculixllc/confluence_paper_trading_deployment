"""
Regime split of the baseline backtest trades (discovery sample). Labels each trade by regime at its entry bar
using only data up to that bar. Run backtest_book2.py --compare --out-json compare_full.json first.
"""
import argparse, json, os, sys, numpy as np, pandas as pd
sys.dont_write_bytecode=True
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument("--csv",default="eur_usd_15m_2023-10_to_2026-10.csv")
ap.add_argument("--compare-json",default="compare_full.json")
ap.add_argument("--out-dir",default=".")
args=ap.parse_args(); CSV=args.csv
d=pd.read_csv(CSV); d["time"]=pd.to_datetime(d["time"],utc=True).dt.tz_convert("America/New_York"); d=d.set_index("time")
c=d.Close
# --- regime features, each uses only bars <= t
sma=c.rolling(2000,min_periods=2000).mean()
d["macro_up"]=c>sma
step=c.diff().abs()
d["er480"]=(c-c.shift(480)).abs()/step.rolling(480).sum()          # 5-day efficiency ratio: 1=straight line, 0=chop
tr=pd.concat([d.High-d.Low,(d.High-c.shift()).abs(),(d.Low-c.shift()).abs()],axis=1).max(axis=1)
d["atr96"]=tr.rolling(96).mean()/0.0001                              # 1-day ATR in pips
d["ret5d"]=(c-c.shift(480))/0.0001                                   # signed 5-day move in pips
def terc(s):
    q1,q2=s.quantile([1/3,2/3]); return pd.cut(s,[-np.inf,q1,q2,np.inf],labels=["low","mid","high"])
d["er_b"]=terc(d.er480.dropna().reindex(d.index)); d["atr_b"]=terc(d.atr96.dropna().reindex(d.index))
h=d.index.hour; d["session"]=np.select([(h>=19)|(h<2),(h>=2)&(h<8),(h>=8)&(h<12)],["Asia(19-02ET)","London(02-08ET)","NY-open(08-12ET)"],"NY-pm(12-19ET)")

r=json.load(open(args.compare_json))
t=pd.DataFrame(r["baseline"]["trades"]); t["entry_time"]=pd.to_datetime(t.entry_time,utc=True).dt.tz_convert("America/New_York")
f=d[["macro_up","er_b","atr_b","session","ret5d"]].reindex(t.entry_time).reset_index(drop=True)
t=pd.concat([t.reset_index(drop=True),f],axis=1)
t["with_macro"]=np.where(((t.direction=="long")&t.macro_up)|((t.direction=="short")&~t.macro_up.astype(bool)),"with macro trend","against macro trend")
t["with_5d"]=np.where(((t.direction=="long")&(t.ret5d>0))|((t.direction=="short")&(t.ret5d<0)),"with 5d move","against 5d move")
t["macro"]=np.where(t.macro_up,"macro uptrend","macro downtrend")
t["half"]=np.where(t.entry_time<pd.Timestamp("2025-07-01",tz="America/New_York"),"H1","H2")
rng=np.random.default_rng(1)
def stats(g):
    if len(g)==0: return None
    w=g[g.pnl>0].pnl.sum(); l=abs(g[g.pnl<0].pnl.sum()); R=g.r_multiple.values
    bs=[rng.choice(R,len(R)).mean() for _ in range(2000)]
    return dict(n=len(g),win=round((g.pnl>0).mean()*100),PF=round(w/l,2) if l else np.inf,avgR=round(R.mean(),2),lo=round(np.percentile(bs,5),2),hi=round(np.percentile(bs,95),2))
def table(col,order=None):
    print(f"\n--- by {col}")
    print(f"{'bucket':<22}{'n':>4}{'win%':>6}{'PF':>6}{'avgR':>7}{'90% CI avgR':>16} | {'H1 n/PF':>10}{'H2 n/PF':>10}")
    for k in (order or sorted(t[col].dropna().unique(),key=str)):
        g=t[t[col]==k]; s=stats(g)
        if not s: continue
        hs=[stats(g[g.half==h]) for h in ("H1","H2")]
        hh=[f"{x['n']}/{x['PF']}" if x else "-" for x in hs]
        print(f"{str(k):<22}{s['n']:>4}{s['win']:>6}{s['PF']:>6}{s['avgR']:>7}{'['+str(s['lo'])+','+str(s['hi'])+']':>16} | {hh[0]:>10}{hh[1]:>10}")
print("ALL baseline:",stats(t))
table("macro"); table("with_macro"); table("with_5d"); table("er_b",["low","mid","high"]); table("atr_b",["low","mid","high"]); table("session")
t["dir_x_macro"]=t.direction+" / "+t.macro; table("dir_x_macro")

# market character per year (explains 2024/25 vs 2026?)
print("\n--- market character by year (all bars)")
y=d.groupby(d.index.year)
print(pd.DataFrame({"EURUSD move(pips)":y.Close.last().sub(y.Close.first())/0.0001,"mean ER480":y.er480.mean().round(3),"mean ATR96(pips)":y.atr96.mean().round(1),"% bars macro-up":(y.macro_up.mean()*100).round(0)}).round(1).to_string())
print("\n--- regime mix of the 82 trades vs all bars (er_b / atr_b)")
for col in ("er_b","atr_b"): print(col,"trades",t[col].value_counts(normalize=True).round(2).to_dict(),"| bars",d[col].value_counts(normalize=True).round(2).to_dict())
t.to_pickle(os.path.join(args.out_dir,"trades_regime.pkl"))
