# -*- coding: utf-8 -*-
"""
株シグナル通知bot（中長期・日米対応）
=====================================
毎朝、日経225 + 日経連続増配株指数 + S&P500 + 米配当貴族をスキャンし、
ファンダメンタル(カテゴリ別基準 + 罠フィルター) × テクニカル(買いタイミング)
の両方を満たした銘柄を、高配当→バリュー→ディフェンシブ→グロースの順に
各カテゴリ上位5銘柄まで通知する。

※このツールは「条件に合致した銘柄の事実ベースの抽出」であり、投資助言ではない。
  最終的な投資判断は必ず自分で行うこと。
"""

import os
import re
import sys
import time
import smtplib
import traceback
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta
from io import StringIO

import requests
import numpy as np
import pandas as pd
import yfinance as yf

# =====================================================================
# 設定（数値基準はここを書き換えるだけで調整できる）
# =====================================================================
CONFIG = {
    # --- 高配当 ---
    "div_yield_min_jp": 0.035,     # 日本株: 配当利回り3.5%以上
    "div_yield_min_us": 0.030,     # 米国株: 配当利回り3.0%以上
    "div_yield_danger": 0.065,     # 6.5%超は減配予備軍として除外(高配当の罠対策)
    "payout_ratio_max": 0.70,      # 配当性向70%未満
    "div_cut_lookback_years": 5,   # 過去5年(完全な暦年)で減配なし
    "div_cut_tolerance": 0.98,     # 前年比98%以上なら「減配なし」とみなす(端数対策)

    # --- バリュー ---
    "value_per_max": 15.0,         # PER15倍未満
    "value_pbr_max": 1.3,          # PBR1.3倍未満
    "graham_mix_max": 22.5,        # グレアム・ミックス係数 PER×PBR≦22.5
    "use_sector_relative": True,   # 業種平均PERとの比較も行う

    # --- ディフェンシブ ---
    "defensive_sectors": ["Consumer Defensive", "Healthcare",
                          "Utilities", "Communication Services"],
    "defensive_industry_keywords": ["Railroad"],  # 鉄道はIndustrials内なのでキーワードで拾う
    "beta_max": 0.8,
    "margin_stability_range": 0.08,  # 営業利益率の変動幅が8pp未満なら安定とみなす

    # --- グロース ---
    "growth_revenue_cagr_min": 0.15,  # 売上CAGR15%以上(直近3年)
    "peg_max": 1.5,                    # PEGレシオ1.5倍以下

    # --- 罠フィルター(全カテゴリ共通) ---
    "roe_min": 0.08,               # ROE8%以上(伊藤レポート基準)
    "equity_ratio_min": 0.40,      # 自己資本比率40%以上
    # 金融・公益はビジネスモデル上レバレッジが高いので自己資本比率チェックを免除
    "equity_ratio_exempt_sectors": ["Financial Services", "Utilities"],
    # データ欠損時の扱い: False=欠損は「未確認」として通過(注記付き) / True=欠損は不合格
    "strict_missing": False,

    # --- テクニカル(買いタイミング判定・全カテゴリ共通) ---
    "rsi_low": 30,
    "rsi_high": 60,
    "sma_slope_lookback": 20,      # 50日線が20営業日前より上なら「上向き」

    # --- 出力 ---
    "top_n_per_category": 5,
    "request_sleep": 0.3,          # yfinanceへの連続リクエストの間隔(秒)
}

CATEGORY_ORDER = ["高配当", "バリュー", "ディフェンシブ", "グロース"]

# =====================================================================
# ユニバース構築
# =====================================================================

def fetch_wikipedia_tables(url):
    """WikipediaページをUser-Agent付きで取得してテーブル一覧を返す(403対策)"""
    html = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    return pd.read_html(StringIO(html))


def fetch_sp500_tickers():
    """WikipediaからS&P500構成銘柄を取得"""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = fetch_wikipedia_tables(url)
        symbols = tables[0]["Symbol"].astype(str).str.replace(".", "-", regex=False)
        return sorted(set(symbols.tolist()))
    except Exception as e:
        print(f"[WARN] S&P500リスト取得失敗: {e}")
        return []


def fetch_dividend_aristocrats():
    """Wikipediaから米配当貴族(25年以上連続増配)を取得"""
    try:
        url = "https://en.wikipedia.org/wiki/S%26P_500_Dividend_Aristocrats"
        tables = fetch_wikipedia_tables(url)
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            for key in ("ticker", "symbol"):
                if any(key in c for c in cols):
                    col = t.columns[[key in str(c).lower() for c in t.columns].index(True)]
                    syms = t[col].astype(str).str.replace(".", "-", regex=False)
                    return sorted(set(syms.tolist()))
        return []
    except Exception as e:
        print(f"[WARN] 配当貴族リスト取得失敗: {e}")
        return []


def fetch_nikkei225_tickers():
    """英語版Wikipediaの日経225ページから4桁コードを抽出。失敗時はローカルCSVにフォールバック"""
    codes = []
    try:
        url = "https://en.wikipedia.org/wiki/Nikkei_225"
        html = requests.get(url, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"}).text
        codes = sorted(set(re.findall(r"TYO:\s*(\d{4})", html)))
    except Exception as e:
        print(f"[WARN] 日経225スクレイプ失敗: {e}")
    if len(codes) < 150:  # 取得が不完全ならフォールバック
        print("[INFO] 日経225はローカルCSV(universe/nikkei225_fallback.csv)を使用")
        codes = load_local_codes("universe/nikkei225_fallback.csv")
    return [f"{c}.T" for c in codes]


def load_local_codes(path):
    """ローカルCSV(1列目=証券コード)を読む"""
    try:
        df = pd.read_csv(path, dtype=str, comment="#")
        return sorted(set(df.iloc[:, 0].str.strip().tolist()))
    except Exception as e:
        print(f"[WARN] {path} 読み込み失敗: {e}")
        return []


def build_universe():
    """ticker -> {'market': 'JP'/'US', 'sources': set} の辞書を返す"""
    universe = {}

    def add(tickers, market, source):
        for t in tickers:
            if not t or t == "nan":
                continue
            universe.setdefault(t, {"market": market, "sources": set()})
            universe[t]["sources"].add(source)

    add(fetch_nikkei225_tickers(), "JP", "日経225")
    jp_growers = [f"{c}.T" for c in load_local_codes("universe/jp_dividend_growers.csv")]
    add(jp_growers, "JP", "連続増配")
    add(fetch_sp500_tickers(), "US", "S&P500")
    add(fetch_dividend_aristocrats(), "US", "配当貴族")

    print(f"[INFO] ユニバース合計: {len(universe)}銘柄")
    return universe

# =====================================================================
# テクニカル指標
# =====================================================================

def calc_rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder方式のRSI(14)。直近値を返す"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else np.nan


def technical_check(close: pd.Series):
    """テクニカル合格判定。(合格bool, 詳細dict) を返す"""
    close = close.dropna()
    if len(close) < 220:
        return False, {}
    sma200 = close.rolling(200).mean()
    sma50 = close.rolling(50).mean()
    price = float(close.iloc[-1])
    rsi = calc_rsi(close)
    lb = CONFIG["sma_slope_lookback"]
    cond_trend = price > float(sma200.iloc[-1])
    cond_rsi = CONFIG["rsi_low"] <= rsi <= CONFIG["rsi_high"]
    cond_slope = float(sma50.iloc[-1]) > float(sma50.iloc[-1 - lb])
    detail = {"price": price, "rsi": round(rsi, 1),
              "above_sma200": cond_trend, "sma50_up": cond_slope}
    return (cond_trend and cond_rsi and cond_slope), detail

# =====================================================================
# ファンダメンタルズ取得・判定
# =====================================================================

def safe_get(info: dict, key: str):
    v = info.get(key)
    if v in (None, "None", "Infinity") or (isinstance(v, float) and np.isnan(v)):
        return None
    return v


def check_or_flag(condition, value, flags, label):
    """欠損データの扱い: strict_missingに従う。合格ならTrue"""
    if value is None:
        if CONFIG["strict_missing"]:
            return False
        flags.append(f"{label}未確認")
        return True
    return condition


def dividend_not_cut(tkr: yf.Ticker):
    """過去N年(完全な暦年)で減配がないか。(bool or None)"""
    try:
        div = tkr.dividends
        if div is None or len(div) == 0:
            return None
        annual = div.groupby(div.index.year).sum()
        this_year = datetime.now().year
        annual = annual[annual.index < this_year]  # 進行中の年は除外
        n = CONFIG["div_cut_lookback_years"]
        if len(annual) < n + 1:
            return None
        recent = annual.iloc[-(n + 1):]
        ratios = recent.values[1:] / recent.values[:-1]
        return bool((ratios >= CONFIG["div_cut_tolerance"]).all())
    except Exception:
        return None


def get_statement_metrics(tkr: yf.Ticker):
    """財務諸表から 自己資本比率 / 営業CF配当カバー / 売上CAGR / 営業増益 / 利益率安定性 を計算"""
    m = {"equity_ratio": None, "cf_coverage": None, "rev_cagr": None,
         "op_income_up": None, "margin_stable": None}
    try:
        bs = tkr.balance_sheet
        if bs is not None and not bs.empty:
            ta = bs.loc["Total Assets"].iloc[0] if "Total Assets" in bs.index else None
            eq = None
            for k in ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"):
                if k in bs.index:
                    eq = bs.loc[k].iloc[0]
                    break
            if ta and eq and ta > 0:
                m["equity_ratio"] = float(eq) / float(ta)
    except Exception:
        pass
    try:
        cf = tkr.cashflow
        if cf is not None and not cf.empty:
            ocf = cf.loc["Operating Cash Flow"].iloc[0] if "Operating Cash Flow" in cf.index else None
            paid = None
            for k in ("Cash Dividends Paid", "Common Stock Dividend Paid"):
                if k in cf.index:
                    paid = cf.loc[k].iloc[0]
                    break
            if ocf is not None and paid not in (None, 0) and abs(paid) > 0:
                m["cf_coverage"] = float(ocf) / abs(float(paid))
    except Exception:
        pass
    try:
        inc = tkr.income_stmt
        if inc is not None and not inc.empty:
            if "Total Revenue" in inc.index:
                rev = inc.loc["Total Revenue"].dropna()
                if len(rev) >= 4 and rev.iloc[3] > 0:
                    m["rev_cagr"] = (rev.iloc[0] / rev.iloc[3]) ** (1 / 3) - 1
                elif len(rev) >= 3 and rev.iloc[2] > 0:
                    m["rev_cagr"] = (rev.iloc[0] / rev.iloc[2]) ** (1 / 2) - 1
            if "Operating Income" in inc.index and "Total Revenue" in inc.index:
                oi = inc.loc["Operating Income"].dropna()
                rev = inc.loc["Total Revenue"].dropna()
                if len(oi) >= 2:
                    m["op_income_up"] = bool(oi.iloc[0] > oi.iloc[1])
                n = min(len(oi), len(rev))
                if n >= 3:
                    margins = (oi.iloc[:n].values / rev.iloc[:n].values)
                    m["margin_stable"] = bool((margins > 0).all() and
                                              (margins.max() - margins.min()) < CONFIG["margin_stability_range"])
    except Exception:
        pass
    return m


def classify(ticker, market, info, tkr, sector_pe_avg):
    """1銘柄をカテゴリ判定。該当カテゴリのリスト[(カテゴリ, スコア, 理由, 注記)]を返す"""
    results = []
    sector = safe_get(info, "sector") or "不明"
    industry = safe_get(info, "industry") or ""
    pe = safe_get(info, "trailingPE")
    pbr = safe_get(info, "priceToBook")
    roe = safe_get(info, "returnOnEquity")
    beta = safe_get(info, "beta")
    dy = safe_get(info, "dividendYield")
    if dy is not None and dy > 1:  # yfinanceは%表記(3.5)と小数(0.035)が混在するため正規化
        dy = dy / 100.0
    payout = safe_get(info, "payoutRatio")

    stm = get_statement_metrics(tkr)
    flags = []

    # ---- 罠フィルター(全カテゴリ共通) ----
    if not check_or_flag(roe is not None and roe >= CONFIG["roe_min"], roe, flags, "ROE"):
        return []
    if sector not in CONFIG["equity_ratio_exempt_sectors"]:
        er = stm["equity_ratio"]
        if not check_or_flag(er is not None and er >= CONFIG["equity_ratio_min"], er, flags, "自己資本比率"):
            return []

    # ---- 高配当 ----
    ymin = CONFIG["div_yield_min_jp"] if market == "JP" else CONFIG["div_yield_min_us"]
    if dy is not None and ymin <= dy <= CONFIG["div_yield_danger"]:
        ok_payout = check_or_flag(payout is not None and 0 < payout < CONFIG["payout_ratio_max"],
                                  payout, flags, "配当性向")
        nocut = dividend_not_cut(tkr)
        ok_cut = check_or_flag(nocut is True, nocut, flags, "減配履歴")
        cov = stm["cf_coverage"]
        ok_cov = check_or_flag(cov is not None and cov >= 1.0, cov, flags, "営業CFカバー")
        if ok_payout and ok_cut and ok_cov:
            reason = f"利回り{dy*100:.1f}%"
            if payout is not None:
                reason += f" / 性向{payout*100:.0f}%"
            if cov is not None:
                reason += f" / CFカバー{cov:.1f}倍"
            results.append(("高配当", dy, reason, list(flags)))

    # ---- バリュー ----
    if pe is not None and pbr is not None and pe > 0 and pbr > 0:
        cond = (pe < CONFIG["value_per_max"] and pbr < CONFIG["value_pbr_max"]
                and pe * pbr <= CONFIG["graham_mix_max"]
                and roe is not None and roe >= CONFIG["roe_min"])
        if cond and CONFIG["use_sector_relative"]:
            avg = sector_pe_avg.get((market, sector))
            cond = avg is None or pe < avg
        if cond:
            reason = f"PER{pe:.1f} / PBR{pbr:.2f} / ミックス{pe*pbr:.1f} / ROE{roe*100:.1f}%"
            results.append(("バリュー", -(pe * pbr), reason, list(flags)))

    # ---- ディフェンシブ ----
    is_def_sector = (sector in CONFIG["defensive_sectors"] or
                     any(k.lower() in industry.lower() for k in CONFIG["defensive_industry_keywords"]))
    if is_def_sector:
        ok_beta = check_or_flag(beta is not None and beta < CONFIG["beta_max"], beta, flags, "ベータ")
        ok_stab = check_or_flag(stm["margin_stable"] is True, stm["margin_stable"], flags, "利益率安定性")
        if ok_beta and ok_stab:
            b = beta if beta is not None else 0.8
            reason = f"{sector} / β{b:.2f}" if beta is not None else f"{sector} / β不明"
            results.append(("ディフェンシブ", -b, reason, list(flags)))

    # ---- グロース ----
    cagr = stm["rev_cagr"]
    if cagr is not None and cagr >= CONFIG["growth_revenue_cagr_min"]:
        ok_oi = check_or_flag(stm["op_income_up"] is True, stm["op_income_up"], flags, "営業増益")
        peg = safe_get(info, "pegRatio")
        if peg is None:
            eg = safe_get(info, "earningsGrowth")
            if pe is not None and eg is not None and eg > 0:
                peg = pe / (eg * 100)
        ok_peg = check_or_flag(peg is not None and 0 < peg <= CONFIG["peg_max"], peg, flags, "PEG")
        if ok_oi and ok_peg:
            reason = f"売上CAGR{cagr*100:.0f}%"
            if peg is not None:
                reason += f" / PEG{peg:.2f}"
            results.append(("グロース", cagr, reason, list(flags)))

    return results

# =====================================================================
# メイン処理
# =====================================================================

def run():
    universe = build_universe()
    tickers = list(universe.keys())
    if not tickers:
        notify("株シグナルbot エラー", "ユニバースの取得に失敗しました。")
        return

    # --- ステージ1: 株価一括取得 & テクニカル判定 ---
    print("[INFO] 株価データ一括取得中...")
    px = yf.download(tickers, period="2y", interval="1d",
                     group_by="ticker", auto_adjust=True,
                     threads=True, progress=False)
    tech_pass = {}
    for t in tickers:
        try:
            close = px[t]["Close"] if isinstance(px.columns, pd.MultiIndex) else px["Close"]
            ok, detail = technical_check(close)
            if ok:
                tech_pass[t] = detail
        except Exception:
            continue
    print(f"[INFO] テクニカル合格: {len(tech_pass)}銘柄")

    # --- ステージ2: 合格銘柄のみ企業情報を取得 ---
    infos = {}
    for i, t in enumerate(tech_pass):
        try:
            infos[t] = yf.Ticker(t).info
        except Exception:
            continue
        time.sleep(CONFIG["request_sleep"])
        if (i + 1) % 50 == 0:
            print(f"[INFO] 企業情報取得 {i+1}/{len(tech_pass)}")

    # 業種平均PER(市場×セクター別)を計算
    rows = []
    for t, info in infos.items():
        pe = safe_get(info, "trailingPE")
        sec = safe_get(info, "sector")
        if pe and sec and 0 < pe < 200:
            rows.append((universe[t]["market"], sec, pe))
    sector_pe_avg = {}
    if rows:
        df = pd.DataFrame(rows, columns=["market", "sector", "pe"])
        sector_pe_avg = df.groupby(["market", "sector"])["pe"].mean().to_dict()

    # --- ステージ3: カテゴリ判定 ---
    picks = {c: [] for c in CATEGORY_ORDER}
    for t, info in infos.items():
        try:
            tkr = yf.Ticker(t)
            for cat, score, reason, flags in classify(t, universe[t]["market"], info, tkr, sector_pe_avg):
                picks[cat].append({
                    "ticker": t,
                    "name": safe_get(info, "shortName") or safe_get(info, "longName") or t,
                    "market": universe[t]["market"],
                    "score": score,
                    "reason": reason,
                    "flags": flags,
                    "tech": tech_pass[t],
                    "sources": "/".join(sorted(universe[t]["sources"])),
                })
            time.sleep(CONFIG["request_sleep"])
        except Exception:
            continue

    # --- 通知本文の組み立て ---
    today = datetime.now().strftime("%Y/%m/%d")
    lines = [f"■ 株シグナル日報 {today}",
             f"（ユニバース{len(tickers)}銘柄 → テクニカル合格{len(tech_pass)}銘柄）", ""]
    n = CONFIG["top_n_per_category"]
    total = 0
    for cat in CATEGORY_ORDER:
        items = sorted(picks[cat], key=lambda x: x["score"], reverse=True)[:n]
        lines.append(f"◆ {cat}（{len(picks[cat])}銘柄合致 / 上位{len(items)}件）")
        if not items:
            lines.append("  本日の該当なし")
        for it in items:
            total += 1
            tech = it["tech"]
            flag_txt = f"（注: {', '.join(it['flags'])}）" if it["flags"] else ""
            lines.append(f"・{it['name']} [{it['ticker']}] {it['market']} <{it['sources']}>")
            lines.append(f"    {it['reason']}")
            lines.append(f"    株価{tech['price']:.1f} / RSI{tech['rsi']} / 200日線上&50日線上向き {flag_txt}")
        lines.append("")
    lines.append("―――")
    lines.append("※本通知は設定条件に合致した銘柄の機械的な抽出であり、投資助言・買い推奨ではありません。")
    lines.append("※投資判断はご自身の責任で。気になる銘柄はClaudeに貼って深掘りしてな。")
    body = "\n".join(lines)
    print(body)

    # レポートをファイルに保存(GitHub Actionsがリポジトリにコミットし、
    # Claudeのスケジュールタスクが毎朝読み取る)
    with open("latest_report.md", "w", encoding="utf-8") as f:
        f.write(body)

    notify(f"【株シグナル】{today} 抽出{total}銘柄", body)

# =====================================================================
# 通知(Gmail / Discord 切替式)
# =====================================================================

def notify(subject: str, body: str):
    channel = os.environ.get("NOTIFY_CHANNEL", "none").lower()
    if channel == "none":
        print("[INFO] 通知チャネルnone: レポートファイル出力のみ(Claudeスケジュールタスク連携用)")
        return
    sent = False
    if channel in ("email", "both"):
        sent = send_email(subject, body) or sent
    if channel in ("discord", "both"):
        sent = send_discord(subject, body) or sent
    if not sent:
        print("[ERROR] 通知の送信に失敗しました。Secretsの設定を確認してください。")


def send_email(subject, body):
    addr = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        print("[WARN] Gmailの設定(GMAIL_ADDRESS / GMAIL_APP_PASSWORD)がありません")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = addr
        msg["To"] = addr
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(addr, pw)
            s.send_message(msg)
        print("[INFO] メール送信完了")
        return True
    except Exception as e:
        print(f"[ERROR] メール送信失敗: {e}")
        return False


def send_discord(subject, body):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[WARN] DISCORD_WEBHOOK_URL がありません")
        return False
    try:
        text = f"**{subject}**\n{body}"
        # Discordは1メッセージ2000文字制限があるため分割送信
        for i in range(0, len(text), 1900):
            requests.post(url, json={"content": text[i:i + 1900]}, timeout=30)
            time.sleep(0.5)
        print("[INFO] Discord送信完了")
        return True
    except Exception as e:
        print(f"[ERROR] Discord送信失敗: {e}")
        return False


if __name__ == "__main__":
    try:
        run()
    except Exception:
        err = traceback.format_exc()
        print(err)
        notify("【株シグナル】実行エラー", f"botの実行中にエラーが発生しました:\n{err[-1500:]}")
        sys.exit(1)
