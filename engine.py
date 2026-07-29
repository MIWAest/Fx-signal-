# -*- coding: utf-8 -*-
"""
帯FX 深逆張りバスケット — 毎日のシグナルエンジン
ルールカード(2026-07-29)を そのまま機械化。
使い方: python3 engine.py   (1日1回・日足確定後=日本の朝がおすすめ)
  → 「今日の指示」を表示し、紙台帳(ledger.csv)とポジ状態(state.json)を自動更新。
  実際の発注は MIWA が マイクロ口座で。このスクリプトは"指示係"であって発注はしない。
"""
import json, os, sys, datetime as dt
import urllib.request
import yfinance as yf, pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
LEDGER = os.path.join(HERE, "ledger.csv")
CONFIG = os.path.join(HERE, "config.json")

def discord_webhook():
    if os.environ.get("FX_DISCORD_WEBHOOK"): return os.environ["FX_DISCORD_WEBHOOK"]
    if os.path.exists(CONFIG):
        try: return json.load(open(CONFIG,encoding="utf-8")).get("discord_webhook")
        except Exception: return None
    return None

def post_discord(msg):
    url=discord_webhook()
    if not url: return False
    url=url.replace("discordapp.com","discord.com")
    try:
        data=json.dumps({"content":msg}).encode()
        req=urllib.request.Request(url, data=data, headers={
            "Content-Type":"application/json",
            "User-Agent":"fx-signal-engine (https://localhost, 1.0)"})
        urllib.request.urlopen(req, timeout=15); return True
    except Exception as e:
        print("  (Discord送信失敗:",e,")"); return False

# --- AUD系の金利差門番メモ(判定はMIWA・これは確認材料の自動添付) ---
def _fred(sid):
    import io
    url=f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
    req=urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    raw=urllib.request.urlopen(req,timeout=25).read().decode()
    df=pd.read_csv(io.StringIO(raw)); df.columns=["date","val"]
    df["date"]=pd.to_datetime(df["date"]); df["val"]=pd.to_numeric(df["val"],errors="coerce")
    return df.dropna().set_index("date")["val"]

def aud_rate_gate(pair):
    # AUD/NZD=豪-NZ, AUD/CAD=豪-加 の金利差フラット判定を"メモ"で返す
    # ※OECD即時金利(IRSTCI01)はNZが2024-12で打ち切り→3ヶ月銀行間(IR3TIB01・全て現行)に差替
    #   注: backtestは即時金利で検証済。BOT化前に3ヶ月物で門番を再検証すること。
    try:
        a=_fred("IR3TIB01AUM156N")
        b=_fred("IR3TIB01NZM156N" if pair=="AUD/NZD" else "IR3TIB01CAM156N")
        d=pd.concat({"a":a,"b":b},axis=1).dropna(); diff=(d["a"]-d["b"])
        cur=float(diff.iloc[-1]); chg=float(diff.iloc[-1]-diff.iloc[max(0,len(diff)-7)])
        latest=diff.index[-1]
        flat=abs(chg)<0.25
        note=f"金利差 現在{cur:+.2f}%・約6M変化{chg:+.2f}% → " + ("フラット✅ 門番GO" if flat else "割れ中⚠ 見送り推奨")
        import datetime as _dt
        if (_dt.date.today()-latest.date()).days>100:
            note+=f"（⚠自動データが{latest.date()}まで＝古い。現在のRBA/RBNZ実値で要確認）"
        return note
    except Exception:
        return "金利差の自動取得に失敗→手動でRBA vs RBNZ(orBoC)を確認して"

# --- ルールカードの定数(検証済み) ---
ACTIVE = {  # 売買する6ペア (yahooティッカー: 表示名)
    "AUDNZD=X":"AUD/NZD", "EURCHF=X":"EUR/CHF", "EURGBP=X":"EUR/GBP",
    "AUDCAD=X":"AUD/CAD", "EURNOK=X":"EUR/NOK", "EURSEK=X":"EUR/SEK",
}
WATCH = {"USDCAD=X":"USD/CAD"}   # 監視のみ(弱い・自動シグナルは出さない)
ENTRY_LONG, ENTRY_SHORT = 25, 75   # RSI<=25で買い / >=75で売り
EXIT_RSI, MAX_DAYS, STOP = 50, 20, 0.02
COST, SWAP = 0.0008, 0.00005       # 紙台帳の損益計算用(実口座は実費)

def rsi14(c):
    d=c.diff(); up=d.clip(lower=0).ewm(alpha=1/14,adjust=False).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    return 100-100/(1+up/dn)

def load_prices(tickers):
    px=yf.download(list(tickers), period="6mo", interval="1d",
                   auto_adjust=False, progress=False)
    close=px["Close"] if "Close" in px.columns.get_level_values(0) else px
    if isinstance(close, pd.Series): close=close.to_frame()
    return close.dropna(how="all")

def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {"positions":{}}   # name -> {side, entry, entry_date, days}

def save_state(s): json.dump(s, open(STATE,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

def append_ledger(row):
    head = not os.path.exists(LEDGER)
    with open(LEDGER,"a",encoding="utf-8") as f:
        if head: f.write("close_date,pair,side,entry_date,entry,exit,days,pnl_pct,reason\n")
        f.write(",".join(str(x) for x in row)+"\n")

def main():
    today = dt.date.today().isoformat()
    close = load_prices(list(ACTIVE)+list(WATCH))
    st = load_state()
    opens, closes, holds, rows = [], [], [], []

    # RSI 最新値を全ペア算出
    rsis, lasts, series = {}, {}, {}
    for tk,name in {**ACTIVE, **WATCH}.items():
        if tk not in close.columns: continue
        c=close[tk].dropna()
        if len(c)<20: continue
        rsis[name]=float(rsi14(c).iloc[-1]); lasts[name]=float(c.iloc[-1]); series[name]=c

    # 1) 既存ポジの出口チェック
    for name, pos in list(st["positions"].items()):
        if name not in rsis: continue
        r=rsis[name]; p=lasts[name]; side=pos["side"]
        c=series[name]
        # 保有日数(データ上の営業日で数える)
        held=int((c.index > pd.Timestamp(pos["entry_date"])).sum())
        cur=side*(p/pos["entry"]-1)
        reason=None
        if cur<=-STOP: reason="stop(-2%)"
        elif (side==1 and r>=EXIT_RSI) or (side==-1 and r<=EXIT_RSI): reason="RSI戻り"
        elif held>=MAX_DAYS: reason="20日経過"
        if reason:
            pnl=cur-COST-SWAP*held
            closes.append((name, side, p, round(pnl*100,2), reason))
            append_ledger((today,name,"買" if side==1 else "売",pos["entry_date"],
                           round(pos['entry'],5),round(p,5),held,round(pnl*100,2),reason))
            del st["positions"][name]
        else:
            holds.append((name, side, held, round(cur*100,2), round(r,1)))

    # 2) 新規エントリー(売買6ペアのみ・既にポジ無い所)
    for tk,name in ACTIVE.items():
        if name in st["positions"] or name not in rsis: continue
        r=rsis[name]; p=lasts[name]
        if r<=ENTRY_LONG:
            opens.append((name,1,p,round(r,1))); st["positions"][name]={"side":1,"entry":p,"entry_date":today,"days":0}
        elif r>=ENTRY_SHORT:
            opens.append((name,-1,p,round(r,1))); st["positions"][name]={"side":-1,"entry":p,"entry_date":today,"days":0}

    save_state(st)

    # AUD系が建った時だけ 金利差メモを1回だけ取得(判定はMIWA)
    AUD={"AUD/NZD","AUD/CAD"}
    aud_notes={name:aud_rate_gate(name) for name,side,p,r in opens if name in AUD}

    # 3) 今日の指示を表示
    print("="*54)
    print(f"  帯FX 深逆張りバスケット — 今日の指示 ({today})")
    print("="*54)
    if opens:
        print("\n🟢 新規建て(マイクロ口座で):")
        for name,side,p,r in opens:
            print(f"   {'買い' if side==1 else '売り'}  {name}  @{p:.5f}  (RSI {r})")
            if name in aud_notes: print(f"      🔎 {aud_notes[name]}")
    if closes:
        print("\n🔴 手仕舞い:")
        for name,side,p,pnl,why in closes: print(f"   決済 {name}  @{p:.5f}  損益{pnl:+.2f}%  ({why})")
    if not opens and not closes:
        print("\n   ✋ 今日の売買なし。触らんのが仕事。")
    if holds:
        print("\n📌 保有継続(監視):")
        for name,side,held,cur,r in holds:
            print(f"   {'買' if side==1 else '売'} {name}  {held}日目  含み{cur:+.2f}%  RSI {r}")

    print("\n— RSI 一覧 —")
    for name in list(ACTIVE.values())+list(WATCH.values()):
        if name not in rsis: continue
        r=rsis[name]; tag=""
        if name in WATCH: tag="(監視のみ)"
        elif r<=ENTRY_LONG: tag="🟢売られ過ぎ"
        elif r>=ENTRY_SHORT: tag="🔴買われ過ぎ"
        print(f"   {name:9} {lasts[name]:8.4f}  RSI {r:4.1f}  {tag}")
    print(f"\n  台帳: {LEDGER}\n  ※発注はMIWAが実施。これは指示係。")

    # --- Discord通知: 光った日(建て/手仕舞い)だけ飛ばす ---
    if opens or closes:
        lines=[f"**🎣 帯FX シグナル ({today})**"]
        for name,side,p,r in opens:
            lines.append(f"🟢 **{'買い' if side==1 else '売り'} {name}** @{p:.5f}  (RSI {r}) → マイクロで建て")
            if name in aud_notes:
                lines.append(f"　🔎 AUD系: {aud_notes[name]}")
        for name,side,p,pnl,why in closes:
            lines.append(f"🔴 決済 **{name}** @{p:.5f}  損益{pnl:+.2f}%  ({why})")
        if holds:
            lines.append("— 保有中: "+", ".join(f"{n}({'買' if s==1 else '売'}{h}日)" for n,s,h,_,_ in holds))
        ok=post_discord("\n".join(lines))
        print("  → Discord通知:", "送信済" if ok else "webhook未設定(config.json)")
    elif "--daily" in sys.argv:
        near=[f"{n} RSI{round(rsis[n],1)}" for n in ACTIVE.values() if n in rsis and (rsis[n]<=32 or rsis[n]>=68)]
        ok=post_discord(f"帯FX {today}: 売買なし ✋" + (("  近い→ "+", ".join(near)) if near else ""))
        print("  → Discord(--daily):", "送信成功" if ok else "送信失敗/webhook無し")

    # 診断: webhookが設定されとるか(値は伏せて)
    wh=discord_webhook()
    print("  [診断] webhook設定:", ("あり(...%s)"%wh[-6:]) if wh else "なし(secret/config未設定)")

if __name__=="__main__":
    if "--test" in sys.argv:
        ok=post_discord("✅ 帯FXエンジン → Discord 接続テスト成功。ここに毎日のシグナルが飛ぶで🐕")
        print("接続テスト:", "成功(Discord見て)" if ok else "失敗: config.jsonにwebhook入れてな"); sys.exit()
    main()
