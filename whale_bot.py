#!/usr/bin/env python3
"""
Whale Tracker Coinbase x Bitget

Alur setiap kali jalan (default tiap 30 menit lewat GitHub Actions):
1. Susun daftar koin: listing di Bitget (spot USDT) DAN di Coinbase (pasangan USD).
2. Baca semua transaksi Coinbase baru sejak pemindaian terakhir (berhalaman, tidak ada yang terlewat).
3. Gabungkan potongan transaksi yang terjadi di detik yang sama dan sisi yang sama menjadi satu order agresif.
   Catat order BELI agresif yang nilainya minimal WHALE_MIN_USD.
4. Koin yang punya beli whale dalam 24 jam terakhir DAN harganya belum bergerak (maks +/- 3%)
   dianalisis price action-nya dan diberi skor conviction 0 sampai 100.
5. Skor minimal MIN_CONVICTION: kirim sinyal ke Telegram. Semua kandidat dicatat ke sinyal_log.csv.
6. Sinyal yang sudah berumur 7 hari dinilai hasilnya (kena target 1, kena cut loss, atau tidak keduanya),
   supaya peluang menang yang sebenarnya bisa diukur.

Catatan penting:
- Di API publik Coinbase, kolom "side" adalah sisi MAKER. side == "sell" berarti pembelinya agresif (taker beli).
- Skor conviction adalah skor aturan, BUKAN peluang menang. Peluang menang dibaca dari kolom hasil di log.
"""

import csv
import datetime as dt
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

# ====================== PENGATURAN (boleh diubah) ======================
WHALE_MIN_USD = float(os.getenv("WHALE_MIN_USD", "300000"))     # nilai minimal satu order beli whale
MAX_PRICE_CHANGE_24H = float(os.getenv("MAX_PRICE_CHANGE_24H", "3"))  # batas "harga belum bergerak" (persen)
MIN_CONVICTION = int(os.getenv("MIN_CONVICTION", "50"))         # ambang skor untuk kirim sinyal
WHALE_WINDOW_HOURS = 24          # beli whale dihitung dalam jendela ini
ALERT_COOLDOWN_HOURS = 24        # satu koin paling banyak satu sinyal per 24 jam
EVAL_AFTER_DAYS = 7              # sinyal dinilai hasilnya setelah 7 hari
MAX_PAGES_PER_COIN = 40          # batas halaman riwayat per koin per jalan (40.000 transaksi)
REQUEST_GAP_SEC = 0.22           # jeda antar permintaan ke Coinbase (batas kecepatan)
DRY_RUN = os.getenv("DRY_RUN") == "1"   # 1 = jangan kirim Telegram, cetak saja
MAX_COINS = int(os.getenv("MAX_COINS", "0"))  # 0 = semua (dipakai untuk uji)

# Stablecoin, emas tokenisasi, dan token bungkus tidak relevan untuk sinyal whale
EXCLUDE = {
    "USDT", "USDC", "DAI", "PYUSD", "USD1", "EURC", "FDUSD", "TUSD", "USDS", "GUSD", "RLUSD", "USDE",
    "PAXG", "XAUT", "WBTC", "CBBTC", "CBETH", "WETH", "LSETH", "MSOL", "JITOSOL",
}

CB = "https://api.exchange.coinbase.com"
BITGET_SYMBOLS = "https://api.bitget.com/api/v2/spot/public/symbols"
ROOT = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
STATE_FILE = ROOT / "state.json"
LOG_FILE = ROOT / "sinyal_log.csv"
WIB = dt.timezone(dt.timedelta(hours=7))
LOG_COLUMNS = [
    "waktu_utc", "koin", "harga", "perubahan_24j_pct", "whale_beli_usd", "jumlah_order_whale",
    "porsi_volume_pct", "skor", "dikirim", "masuk_bawah", "cut_loss", "target1", "target2",
    "rasio_untung_rugi", "alasan", "hasil", "hasil_7hari_pct", "naik_maks_pct", "turun_maks_pct",
]

# ============================ HTTP ============================
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "whale-tracker/1.0"
_last_call = [0.0]


def get(url, params=None, tries=6):
    """GET dengan jeda antar permintaan dan coba ulang saat dibatasi (429) atau server error."""
    for attempt in range(tries):
        wait = REQUEST_GAP_SEC - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = SESSION.get(url, params=params, timeout=25)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Gagal mengambil {url}")


# ============================ UTIL ============================
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _id_format(s):
    """Ubah format angka Inggris (1,234.56) ke format Indonesia (1.234,56)."""
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_price(p):
    if p >= 1000:
        s = f"{p:,.0f}"
    elif p >= 1:
        s = f"{p:,.2f}"
    elif p >= 0.01:
        s = f"{p:.4f}"
    else:
        s = f"{p:.8f}".rstrip("0")
    return "$" + _id_format(s)


def fmt_usd(v):
    if v >= 1_000_000:
        return "$" + _id_format(f"{v / 1_000_000:.2f}") + " jt"
    if v >= 1_000:
        return "$" + _id_format(f"{v / 1_000:.0f}") + " rb"
    return "$" + _id_format(f"{v:.0f}")


def fmt_pct(v, sign=True):
    if abs(v) < 0.05:
        v = 0.0
    s = f"{v:+.1f}" if sign and v else f"{v:.1f}"
    return _id_format(s) + "%"


def wib_str(t):
    days = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
    months = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    w = t.astimezone(WIB)
    return f"{days[w.weekday()]} {w.day} {months[w.month - 1]} {w.year}, {w:%H.%M} WIB"


# ============================ STATE & LOG ============================
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


def read_log():
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_log(rows):
    with LOG_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LOG_COLUMNS})


# ============================ TELEGRAM ============================
def discover_chat_id(token):
    """Cari chat id dari pesan terakhir yang dikirim ke bot (hanya dipakai saat TELEGRAM_CHAT_ID belum diisi)."""
    try:
        res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    except requests.RequestException:
        return None
    for upd in reversed(res.get("result", [])):
        chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat", {})
        if chat.get("id"):
            return str(chat["id"])
    return None


def tg_send(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if DRY_RUN or not token:
        print("\n----- PESAN (tidak dikirim) -----\n" + text + "\n---------------------------------")
        return False
    if not chat:
        chat = discover_chat_id(token)
        if not chat:
            print("TELEGRAM_CHAT_ID kosong dan belum ada pesan ke bot. Kirim /start ke bot Anda dulu.")
            return False
        text += (f"\n\n⚙️ Pengaturan: simpan angka {chat} sebagai rahasia TELEGRAM_CHAT_ID di GitHub "
                 "agar pesan berikutnya pasti sampai.")
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
                          timeout=20)
        if r.status_code != 200:
            print("Gagal kirim Telegram:", r.status_code, r.text[:200])
            return False
    return True


# ============================ DATA PASAR ============================
def build_universe():
    bg = get(BITGET_SYMBOLS).json().get("data", [])
    bitget = {}
    for s in bg:
        if s.get("quoteCoin") == "USDT" and s.get("status") == "online":
            bitget[s["baseCoin"].upper()] = s.get("symbol", s["baseCoin"].upper() + "USDT")
    prods = get(f"{CB}/products").json()
    pids = sorted(
        p["id"] for p in prods
        if p.get("quote_currency") == "USD" and p.get("status") == "online"
        and not p.get("trading_disabled")
        and p["base_currency"].upper() in bitget
        and p["base_currency"].upper() not in EXCLUDE
    )
    return pids, bitget


def scan_trades(pid, last_id):
    """Ambil transaksi baru sejak last_id. Kembalikan (transaksi, trade_id terbaru, ada_celah)."""
    trades, after, newest = [], None, None
    for _ in range(MAX_PAGES_PER_COIN):
        params = {"limit": 1000}
        if after:
            params["after"] = after
        r = get(f"{CB}/products/{pid}/trades", params)
        batch = r.json()
        if not batch:
            return trades, newest or last_id, False
        if newest is None:
            newest = batch[0]["trade_id"]
        if last_id is None:            # jalan pertama: cukup satu halaman terbaru
            trades.extend(batch)
            return trades, newest, False
        fresh = [t for t in batch if t["trade_id"] > last_id]
        trades.extend(fresh)
        if len(fresh) < len(batch):    # sudah menyambung dengan pemindaian sebelumnya
            return trades, newest, False
        after = r.headers.get("cb-after")
        if not after:
            return trades, newest, False
    return trades, newest, True        # batas halaman tercapai, mungkin ada celah


def whale_orders(trades):
    """Gabungkan potongan di detik dan sisi yang sama menjadi satu order agresif; kembalikan yang >= ambang."""
    orders = {}
    for t in trades:
        key = (t["time"], t["side"])
        o = orders.setdefault(key, {"time": t["time"], "usd": 0.0, "fills": 0,
                                    "side": "BUY" if t["side"] == "sell" else "SELL"})
        o["usd"] += float(t["price"]) * float(t["size"])
        o["fills"] += 1
    return [o for o in orders.values() if o["usd"] >= WHALE_MIN_USD]


def stats24(pid):
    j = get(f"{CB}/products/{pid}/stats").json()
    return float(j["open"]), float(j["last"]), float(j["volume"])


def candles(pid, granularity, start=None, end=None):
    """Lilin Coinbase: [waktu, low, high, open, close, volume], diurutkan dari lama ke baru."""
    params = {"granularity": granularity}
    if start:
        params["start"], params["end"] = start, end
    rows = get(f"{CB}/products/{pid}/candles", params).json()
    return sorted(rows, key=lambda r: r[0]) if isinstance(rows, list) else []


# ============================ INDIKATOR ============================
def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0))
        losses.append(max(a - b, 0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def btc_regime():
    d = candles("BTC-USD", 86400)
    closes = [r[4] for r in d]
    price = closes[-1]
    e50, s200 = ema(closes, 50), sma(closes, 200)
    return {"price": price, "above_ema50": e50 is not None and price > e50,
            "below_sma200": s200 is not None and price < s200, "ema50": e50, "sma200": s200}


# ============================ ANALISIS & SKOR ============================
def analyze(pid, whales, btc, market=None):
    """Kembalikan dict analisis, atau None jika harga sudah bergerak lebih dari batas."""
    o24, price, vol_base = market or stats24(pid)
    chg = (price - o24) / o24 * 100 if o24 else 0.0
    if abs(chg) > MAX_PRICE_CHANGE_24H:
        return None

    d = candles(pid, 86400)
    h6 = candles(pid, 21600)
    closes = [r[4] for r in d]
    d_low, d_high, d_vol = [r[1] for r in d], [r[2] for r in d], [r[5] for r in d]
    h6_close, h6_low = [r[4] for r in h6], [r[1] for r in h6]

    score, checks = 0, []

    def add(pts, ok, text):
        nonlocal score
        score += pts
        checks.append(("✅" if ok else "❌") + " " + text)

    # 1. Katalis whale
    buys = [w for w in whales if w["side"] == "BUY"]
    sells = [w for w in whales if w["side"] == "SELL"]
    buy_usd, sell_usd = sum(w["usd"] for w in buys), sum(w["usd"] for w in sells)
    vol_usd = vol_base * price
    share = buy_usd / vol_usd * 100 if vol_usd else 0.0
    whale_pts = 15 + (10 if len(buys) >= 3 else 5 if len(buys) == 2 else 0)
    whale_pts += 10 if share >= 5 else 5 if share >= 2 else 0
    score += whale_pts
    whale_notes, blocked = [], False
    if sell_usd >= buy_usd:
        # Katalis pertama batal: whale bersih menjual. Sinyal tidak dikirim berapa pun skornya.
        blocked = True
        whale_notes.append(f"⛔ Whale lebih banyak menjual ({fmt_usd(sell_usd)}) daripada membeli ({fmt_usd(buy_usd)}): katalis batal")
    elif sell_usd >= 0.5 * buy_usd:
        score -= 10
        whale_notes.append(f"❌ Ada jual whale cukup besar ({fmt_usd(sell_usd)}) yang menahan tesis beli")

    # 2. Harga belum bergerak
    score += 10 if abs(chg) <= 1.5 else 5

    # 3. Price action
    s200 = sma(closes, 200)
    if s200 is not None:
        add(10 if price > s200 else 0, price > s200, f"Harga di atas rata rata 200 hari ({fmt_price(s200)})"
            if price > s200 else f"Harga di bawah rata rata 200 hari ({fmt_price(s200)})")
    else:
        e50d = ema(closes, 50)
        if e50d is not None:
            add(10 if price > e50d else 0, price > e50d,
                f"Harga {'di atas' if price > e50d else 'di bawah'} EMA 50 hari ({fmt_price(e50d)}); riwayat < 200 hari")
        else:
            checks.append("⚪ Riwayat harga terlalu pendek untuk menilai tren besar")

    e20, e50 = ema(h6_close, 20), ema(h6_close, 50)
    if e20 is not None and e50 is not None:
        add(8 if e20 > e50 else 0, e20 > e50,
            "Tren menengah naik: EMA 20 di atas EMA 50 (grafik 6 jam)" if e20 > e50
            else "Tren menengah turun: EMA 20 di bawah EMA 50 (grafik 6 jam)")

    if len(h6_low) >= 24:
        hl = min(h6_low[-12:]) > min(h6_low[-24:-12])
        add(8 if hl else 0, hl, "Membentuk higher low dalam 3 hari terakhir" if hl
            else "Belum membentuk higher low (dasar 3 hari terakhir lebih rendah)")

    r = rsi(closes)
    if r is not None:
        if r > 75:
            score -= 10
            checks.append(f"❌ RSI 14 hari {r:.0f}: jenuh beli, rawan koreksi")
        else:
            ok = 40 <= r <= 65
            add(7 if ok else 0, ok, f"RSI 14 hari {r:.0f}" + (": ada ruang naik" if ok else
                                                             (": jenuh jual" if r < 30 else ": momentum lemah")))

    window = min(30, len(d))
    low30, high30 = min(d_low[-window:]), max(d_high[-window:])
    near = price <= low30 * 1.08
    add(7 if near else 0, near, f"Dekat dasar kisaran 30 hari ({fmt_price(low30)})" if near
        else f"Jauh dari dasar kisaran 30 hari ({fmt_price(low30)})")
    rng = (high30 - low30) / low30 * 100 if low30 else 999
    tight = rng <= 25
    add(5 if tight else 0, tight, f"Kisaran 30 hari sempit ({fmt_pct(rng, False)}): pola akumulasi" if tight
        else f"Kisaran 30 hari lebar ({fmt_pct(rng, False)})")

    if len(d_vol) >= 21:
        avg20 = sum(d_vol[-21:-1]) / 20
        vr = vol_base / avg20 if avg20 else 0
        vr_text = _id_format(f"{vr:.1f}")
        add(5 if vr >= 1.2 else 0, vr >= 1.2, f"Volume 24 jam {vr_text}x rata rata 20 hari")

    # 4. Kondisi BTC
    if btc["above_ema50"]:
        score += 10
        btc_text = "BTC di atas EMA 50 harian (mendukung)"
    elif btc["below_sma200"]:
        score -= 10
        btc_text = "BTC di bawah rata rata 200 hari (melawan)"
    else:
        btc_text = "BTC di bawah EMA 50 harian, tapi di atas rata rata 200 hari (netral)"

    # 5. Rencana trading
    support = min(h6_low[-12:]) if len(h6_low) >= 12 else low30
    stop = min(support * 0.98, price * 0.97)
    lookbacks = [14, 30, 90]
    levels = sorted({max(d_high[-n:]) for n in lookbacks if len(d_high) >= 2})
    targets = [lv for lv in levels if lv >= price * 1.03]
    t1 = targets[0] if targets else price * 1.10
    t2 = targets[1] if len(targets) > 1 else t1 + (t1 - price)
    rr = (t1 - price) / (price - stop) if price > stop else 0
    if rr < 1.5:
        score -= 10
        checks.append(f"❌ Rasio untung/rugi hanya 1:{_id_format(f'{rr:.1f}')}: target terlalu dekat dibanding risiko")

    score = max(0, min(100, round(score)))
    return {
        "pid": pid, "coin": pid.split("-")[0], "price": price, "chg": chg, "score": score,
        "checks": checks, "whale_notes": whale_notes, "blocked": blocked, "btc_text": btc_text, "buys": buys, "buy_usd": buy_usd, "sell_usd": sell_usd,
        "share": share, "entry_low": max(support, stop * 1.01), "stop": stop, "t1": t1, "t2": t2, "rr": rr,
    }


def signal_message(a, bitget_symbol):
    lines = [
        f"🐋 SINYAL WHALE | {a['coin']}",
        f"Skor conviction: {a['score']}/100 (ambang {MIN_CONVICTION})",
        f"Waktu: {wib_str(now_utc())}",
        "",
        "Katalis whale (Coinbase)",
    ]
    for w in sorted(a["buys"], key=lambda x: -x["usd"])[:3]:
        lines.append(f"• Order beli agresif {fmt_usd(w['usd'])} ({wib_str(parse_ts(w['time'])).split(', ')[1]})")
    lines.append(f"• Total beli whale 24 jam: {fmt_usd(a['buy_usd'])} ({fmt_pct(a['share'], False)} dari volume 24 jam)")
    lines.append(f"• Harga 24 jam: {fmt_pct(a['chg'])} (belum bergerak)")
    lines += a["whale_notes"]
    lines += ["", "Price action"] + a["checks"]
    down = (a["stop"] / a["price"] - 1) * 100
    rr_text = _id_format(f"{a['rr']:.1f}")
    lines += [
        "",
        "Rencana",
        f"• Masuk: {fmt_price(a['price'])} s.d. {fmt_price(a['entry_low'])}",
        f"• Cut loss: {fmt_price(a['stop'])} ({fmt_pct(down)})",
        f"• Target 1: {fmt_price(a['t1'])} ({fmt_pct((a['t1'] / a['price'] - 1) * 100)})",
        f"• Target 2: {fmt_price(a['t2'])} ({fmt_pct((a['t2'] / a['price'] - 1) * 100)})",
        f"• Rasio untung/rugi 1:{rr_text}",
        "",
        a["btc_text"],
        f"Listing Bitget: {bitget_symbol}",
        "",
        "Skor bukan peluang menang. Ini bahan pertimbangan, bukan saran keuangan.",
    ]
    return "\n".join(lines)


# ============================ EVALUASI HASIL ============================
def evaluate_outcomes(rows):
    """Nilai sinyal yang sudah berumur EVAL_AFTER_DAYS: target 1 atau cut loss yang kena lebih dulu."""
    changed = 0
    for row in rows:
        if row.get("hasil") or not row.get("cut_loss"):
            continue
        t0 = parse_ts(row["waktu_utc"])
        if now_utc() - t0 < dt.timedelta(days=EVAL_AFTER_DAYS):
            continue
        t_end = t0 + dt.timedelta(days=EVAL_AFTER_DAYS)
        try:
            hrs = candles(f"{row['koin']}-USD", 3600, t0.isoformat(), t_end.isoformat())
        except Exception as e:  # koin delisting atau data hilang
            row["hasil"] = f"DATA TIDAK ADA ({e.__class__.__name__})"
            changed += 1
            continue
        if not hrs:
            row["hasil"] = "DATA TIDAK ADA"
            changed += 1
            continue
        entry, stop, t1 = float(row["harga"]), float(row["cut_loss"]), float(row["target1"])
        outcome = "TIDAK KEDUANYA"
        for c in hrs:
            hit_stop, hit_t1 = c[1] <= stop, c[2] >= t1
            if hit_stop and hit_t1:
                outcome = "CUT LOSS (satu lilin dengan target, dianggap kalah)"
                break
            if hit_stop:
                outcome = "CUT LOSS"
                break
            if hit_t1:
                outcome = "TARGET 1"
                break
        row["hasil"] = outcome
        row["hasil_7hari_pct"] = f"{(hrs[-1][4] / entry - 1) * 100:.2f}"
        row["naik_maks_pct"] = f"{(max(c[2] for c in hrs) / entry - 1) * 100:.2f}"
        row["turun_maks_pct"] = f"{(min(c[1] for c in hrs) / entry - 1) * 100:.2f}"
        changed += 1
    return changed


def weekly_report(state, rows, n_coins):
    week = state.get("week", {})
    done = [r for r in rows if r.get("hasil") and r.get("dikirim") == "ya"]
    win = sum(1 for r in done if r["hasil"].startswith("TARGET"))
    lose = sum(1 for r in done if r["hasil"].startswith("CUT"))
    lines = [
        "📋 Laporan mingguan whale tracker",
        f"Bot aktif. Memantau {n_coins} koin (Coinbase dan Bitget).",
        f"Minggu ini: {week.get('whale_buys', 0)} order beli whale ≥ {fmt_usd(WHALE_MIN_USD)}, "
        f"{week.get('candidates', 0)} kandidat dianalisis, {week.get('sent', 0)} sinyal dikirim.",
    ]
    if done:
        lines.append(f"Hasil semua sinyal yang sudah 7 hari: {win} kena target 1, {lose} kena cut loss, "
                     f"{len(done) - win - lose} tidak keduanya. Peluang menang: {win / len(done) * 100:.0f}% "
                     f"dari {len(done)} sinyal.")
    else:
        lines.append("Belum ada sinyal yang berumur 7 hari untuk dinilai.")
    if week.get("gaps"):
        lines.append(f"Catatan: {week['gaps']} kali riwayat koin sangat ramai terpotong (batas halaman).")
    return "\n".join(lines)


# ============================ UTAMA ============================
def main():
    t_start = now_utc()
    state = load_state()
    first_run = not state
    cursors = state.setdefault("cursors", {})
    whales = state.setdefault("whales", {})
    alerted = state.setdefault("alerted", {})
    logged = state.setdefault("logged", {})
    week = state.setdefault("week", {"since": t_start.isoformat()})

    pids, bitget = build_universe()
    if MAX_COINS:
        pids = pids[:MAX_COINS]
    print(f"Memantau {len(pids)} koin. Ambang whale {WHALE_MIN_USD:,.0f} USD.")

    # Langkah 1 sampai 3: pindai transaksi
    cutoff = t_start - dt.timedelta(hours=WHALE_WINDOW_HOURS * 2)
    for i, pid in enumerate(pids, 1):
        try:
            trades, newest, gap = scan_trades(pid, cursors.get(pid))
        except Exception as e:
            print(f"[{pid}] gagal dipindai: {e}")
            continue
        if newest is not None:
            cursors[pid] = newest
        if gap:
            week["gaps"] = week.get("gaps", 0) + 1
        found = whale_orders(trades)
        if found:
            lst = whales.setdefault(pid, [])
            seen = {(w["time"], w["side"]) for w in lst}
            for w in found:
                if (w["time"], w["side"]) not in seen:
                    lst.append({"time": w["time"], "usd": round(w["usd"], 2), "side": w["side"], "fills": w["fills"]})
                    if w["side"] == "BUY":
                        week["whale_buys"] = week.get("whale_buys", 0) + 1
                    print(f"[{pid}] whale {w['side']} {w['usd']:,.0f} USD pada {w['time']} ({w['fills']} potongan)")
        if i % 50 == 0:
            print(f"... {i}/{len(pids)} koin dipindai")

    # Buang catatan whale yang sudah lama
    for pid in list(whales):
        whales[pid] = [w for w in whales[pid] if parse_ts(w["time"]) >= cutoff]
        if not whales[pid]:
            del whales[pid]

    # Langkah 4 dan 5: analisis kandidat
    rows = read_log()
    window_start = t_start - dt.timedelta(hours=WHALE_WINDOW_HOURS)
    candidates = [pid for pid, lst in whales.items()
                  if any(w["side"] == "BUY" and parse_ts(w["time"]) >= window_start for w in lst)]
    btc = btc_regime() if candidates else None
    for pid in candidates:
        if pid in alerted and t_start - parse_ts(alerted[pid]) < dt.timedelta(hours=ALERT_COOLDOWN_HOURS):
            continue
        recent = [w for w in whales[pid] if parse_ts(w["time"]) >= window_start]
        try:
            a = analyze(pid, recent, btc)
        except Exception as e:
            print(f"[{pid}] analisis gagal: {e}")
            continue
        if a is None:
            print(f"[{pid}] ada beli whale tapi harga sudah bergerak > {MAX_PRICE_CHANGE_24H}% dalam 24 jam, dilewati")
            continue
        week["candidates"] = week.get("candidates", 0) + 1
        print(f"[{pid}] skor {a['score']}")
        send = a["score"] >= MIN_CONVICTION and not a["blocked"]
        newest_whale = max(w["time"] for w in a["buys"])
        if send:
            sent_ok = tg_send(signal_message(a, bitget.get(a["coin"], a["coin"] + "USDT")))
            alerted[pid] = t_start.isoformat()
            week["sent"] = week.get("sent", 0) + (1 if sent_ok or DRY_RUN else 0)
        if send or logged.get(pid) != newest_whale:
            # Catat setiap kandidat sekali per kejadian whale, dan setiap sinyal yang dikirim
            rows.append({
                "waktu_utc": t_start.isoformat(timespec="seconds"), "koin": a["coin"],
                "harga": f"{a['price']:.8g}", "perubahan_24j_pct": f"{a['chg']:.2f}",
                "whale_beli_usd": f"{a['buy_usd']:.0f}", "jumlah_order_whale": len(a["buys"]),
                "porsi_volume_pct": f"{a['share']:.2f}", "skor": a["score"], "dikirim": "ya" if send else "tidak",
                "masuk_bawah": f"{a['entry_low']:.8g}", "cut_loss": f"{a['stop']:.8g}",
                "target1": f"{a['t1']:.8g}", "target2": f"{a['t2']:.8g}", "rasio_untung_rugi": f"{a['rr']:.2f}",
                "alasan": " | ".join(c[2:].strip() for c in a["whale_notes"] + a["checks"]),
            })
            logged[pid] = newest_whale

    # Langkah 6: nilai hasil sinyal lama
    try:
        evaluate_outcomes(rows)
    except Exception as e:
        print("Evaluasi hasil gagal:", e)
    write_log(rows)

    # Pesan pertama dan laporan mingguan (sekaligus tanda bot masih hidup)
    if first_run:
        tg_send(f"✅ Whale tracker aktif.\nMemantau {len(pids)} koin yang listing di Coinbase dan Bitget.\n"
                f"Sinyal dikirim jika ada order beli ≥ {fmt_usd(WHALE_MIN_USD)}, harga 24 jam maks "
                f"±{_id_format(f'{MAX_PRICE_CHANGE_24H:g}')}%, dan skor conviction ≥ {MIN_CONVICTION}.\n"
                "Laporan mingguan dikirim tiap 7 hari.")
        week["since"] = t_start.isoformat()
    elif t_start - parse_ts(week.get("since", t_start.isoformat())) >= dt.timedelta(days=7):
        tg_send(weekly_report(state, rows, len(pids)))
        state["week"] = {"since": t_start.isoformat()}

    state["last_run"] = t_start.isoformat()
    save_state(state)
    print(f"Selesai dalam {(now_utc() - t_start).total_seconds():.0f} detik.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc)
        sys.exit(1)
