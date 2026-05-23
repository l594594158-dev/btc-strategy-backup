#!/usr/bin/env python3
"""v4.1 现货 1m轮询回测 — 模拟实盘每1分钟获取价格"""
import pandas as pd, numpy as np, ta, time

t0 = time.time(); TP=0.025; SL=0.015

print('=== 构建现货K线 (全量) ===')
df = pd.read_pickle('backtest_data/btc_1s_1y.pkl')
df1 = df.resample('1min',label='right',closed='right').agg(
    {'open':'first','high':'max','low':'min','close':'last'}).dropna()
df5 = df.resample('5min',label='right',closed='right').agg(
    {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
df1h = df.resample('1h',label='right',closed='right').agg(
    {'open':'first','high':'max','low':'min','close':'last'}).dropna()
df4h = df.resample('4h',label='right',closed='right').agg(
    {'open':'first','high':'max','low':'min','close':'last'}).dropna()
df1d = df.resample('D',label='right',closed='right').agg(
    {'open':'first','high':'max','low':'min','close':'last'}).dropna()

c1=df1['close'].values; ts1=df1.index; n1=len(c1)
c5=df5['close'].values; v5=df5['volume'].values; ts5=df5.index; n5=len(c5)
print(f'1m:{n1} 5m:{n5} 1h:{len(df1h)} 4h:{len(df4h)} 1d:{len(df1d)}')

# 大周期指标
def calc_tf(cv,hv,lv):
    n=len(cv); cc=np.full(n,np.nan); sc=np.full(n,np.nan); ac=np.full(n,np.nan)
    sf=ta.trend.SMAIndicator(pd.Series(cv),20).sma_indicator().values
    try: af=ta.trend.ADXIndicator(pd.Series(hv),pd.Series(lv),pd.Series(cv),14).adx().values
    except: af=np.full(n,25.0)
    for i in range(20,n): cc[i]=cv[i]; sc[i]=sf[i] if not np.isnan(sf[i]) else np.nan; ac[i]=af[i] if not np.isnan(af[i]) else 25.0
    return cc,sc,ac

c4hc,s4hc,a4hc=calc_tf(df4h['close'].values,df4h['high'].values,df4h['low'].values)
c1dc,s1dc,_=calc_tf(df1d['close'].values,df1d['high'].values,df1d['low'].values)
_,_,a1hc=calc_tf(df1h['close'].values,df1h['high'].values,df1h['low'].values)

def m5(st,vv):
    r=np.full(n5,np.nan)
    for i in range(1,n5): p=np.searchsorted(st,ts5[i],side='right')-1; r[i]=vv[p] if 0<=p<len(vv) else r[i]
    return r

c4h5=m5(df4h.index,c4hc); s4h5=m5(df4h.index,s4hc); a4h5=m5(df4h.index,a4hc)
c1d5=m5(df1d.index,c1dc); s1d5=m5(df1d.index,s1dc); a1h5=m5(df1h.index,a1hc)

# 5m Wilder RSI基准 (14根已关闭5m bar)
ag5=np.full(n5,np.nan); al5=np.full(n5,np.nan)
for i in range(15,n5):
    d=c5[i-1]-c5[i-2]
    if i==15: ag5[i]=np.mean([max(c5[j]-c5[j-1],0)for j in range(1,15)]); al5[i]=np.mean([max(c5[j-1]-c5[j],0)for j in range(1,15)])
    else: ag5[i]=(ag5[i-1]*13+max(d,0))/14; al5[i]=(al5[i-1]*13+max(-d,0))/14

vr5=np.full(n5,1.0)
for i in range(21,n5): a=v5[i-20:i].mean(); vr5[i]=v5[i-1]/a if a>0 else 1.0

# 预映射1m bar所属5m bar
i5_of_1m = np.searchsorted(ts5, ts1, side='right') - 1

# 回测
cut=pd.Timestamp('2026-02-23')
I1S = np.searchsorted(ts1, cut)
print(f'\n1m轮询回测 {n1-I1S} 根...')

lp=None; sp=None; lls=False; lss=False; trades=[]
last_i5 = -1

for i1 in range(I1S, n1):
    i5 = i5_of_1m[i1]
    if i5 < 30: continue
    if i5 != last_i5:
        # 新5m bar: 刷新冻结指标
        last_i5 = i5
        if np.isnan(c4h5[i5]) or np.isnan(s4h5[i5]) or np.isnan(a4h5[i5]): continue
        if np.isnan(c1d5[i5]) or np.isnan(s1d5[i5]) or np.isnan(a1h5[i5]): continue
        hb = c4h5[i5] > s4h5[i5]
        db = c1d5[i5] > s1d5[i5]
        a1v = a1h5[i5]; a4v = a4h5[i5]; vr = vr5[i5]
        bar_open = c5[i5-1] if i5>0 else c5[i5]  # 实际用1m数据里的open
        first_1m_in_bar = True
    
    px = c1[i1]; pxt = ts1[i1]
    
    # 出场
    if lp:
        pnl=(px-lp['ep'])/lp['ep']*100
        if pnl<=-SL*100: trades.append({'d':'L','ets':lp['ts'],'ep':lp['ep'],'xts':pxt,'xt':'SL','pnl':-1.5}); lp=None; lls=False
        elif pnl>=TP*100: trades.append({'d':'L','ets':lp['ts'],'ep':lp['ep'],'xts':pxt,'xt':'TP','pnl':2.5}); lp=None; lls=False
    if sp:
        pnl=(sp['ep']-px)/sp['ep']*100
        if pnl<=-SL*100: trades.append({'d':'S','ets':sp['ts'],'ep':sp['ep'],'xts':pxt,'xt':'SL','pnl':-1.5}); sp=None; lss=False
        elif pnl>=TP*100: trades.append({'d':'S','ets':sp['ts'],'ep':sp['ep'],'xts':pxt,'xt':'TP','pnl':2.5}); sp=None; lss=False

    if first_1m_in_bar:
        first_1m_in_bar = False
        continue
    
    # 七条件短路
    if a1v<=25: continue
    if a4v>=40: continue
    s19=c5[max(0,i5-19):i5].sum()
    sd=(s19+px)/20
    if abs((px-sd)/sd*100)>1.0: continue
    if vr<1.0: continue
    if np.isnan(ag5[i5])or np.isnan(al5[i5]): continue
    # RSI增量 (bar内累计delta)
    bar_delta = px - c5[i5-1]  # vs 5m bar open
    cg=(ag5[i5]*13+max(bar_delta,0))/14; cl=(al5[i5]*13+max(-bar_delta,0))/14
    if cl<=0: continue
    rsi=100-100/(1+cg/cl)

    ln=hb and db and rsi>40
    if ln and not lls and lp is None and sp is None: lp={'ep':px,'ts':pxt}
    lls=ln
    sn=(not hb)and(not db)and rsi<60
    if sn and not lss and sp is None and lp is None: sp={'ep':px,'ts':pxt}
    lss=sn

# 统计
df_t=pd.DataFrame(trades)
total_all = len(df_t)
df_t['ets_dt']=pd.to_datetime(df_t['ets'])
R=df_t[df_t['ets_dt']>=cut]
w=(R['pnl']>0).sum(); wr=w/len(R)*100
tp_c=(R['xt']=='TP').sum(); sl_c=(R['xt']=='SL').sum(); nL=len(R[R['d']=='L']); nS=len(R[R['d']=='S'])

print(f'\n=== 1m轮询回测 近3月 ===')
print(f'{len(R)}笔 WR={wr:.1f}% PnL={R["pnl"].sum():+.1f}%')
print(f'L:{nL} S:{nS} TP:{tp_c} SL:{sl_c}')
if nL: ldf=R[R['d']=='L']; print(f'LONG WR={(ldf["pnl"]>0).sum()/nL*100:.0f}% PnL={ldf["pnl"].sum():.1f}%')
if nS: sdf=R[R['d']=='S']; print(f'SHORT WR={(sdf["pnl"]>0).sum()/nS*100:.0f}% PnL={sdf["pnl"].sum():.1f}%')

R=R.copy(); R['ym']=R['ets_dt'].dt.strftime('%Y-%m'); R=R.sort_values('ets_dt')
for ym in sorted(R['ym'].unique()):
    s=R[R['ym']==ym]; w2=(s['pnl']>0).sum()
    print(f'\n[{ym}] {len(s)}笔 WR={w2/len(s)*100:.0f}% PnL={s["pnl"].sum():+.1f}%')
    for i,(_,r) in enumerate(s.iterrows(),1):
        print(f'  {i:2d}. {r["d"]} {str(r["ets"])[5:16]} ${r["ep"]:,.0f} -> {r["xt"]}')

print(f'\n耗时:{(time.time()-t0)/60:.1f}min')
