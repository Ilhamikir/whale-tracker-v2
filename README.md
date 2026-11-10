# Whale Tracker Coinbase x Bitget

Bot pemantau order beli whale di Coinbase untuk koin yang juga listing di Bitget. Sinyal dikirim ke Telegram.

## Cara kerja singkat

1. Tiap 30 menit, bot membaca semua transaksi baru di Coinbase untuk sekitar 216 koin yang juga ada di Bitget (spot USDT). Stablecoin dan token bungkus tidak dihitung.
2. Potongan transaksi di detik dan sisi yang sama digabung menjadi satu order agresif. Order **beli** agresif minimal **$300.000** dicatat sebagai beli whale.
3. Koin dengan beli whale dalam 24 jam terakhir dan harga 24 jam yang masih datar (maks **±3%**) dianalisis price action-nya, lalu diberi skor conviction 0 sampai 100.
4. Skor minimal **50** dan whale tidak bersih menjual: sinyal dikirim ke Telegram, satu koin maksimal sekali per 24 jam.
5. Semua kandidat, yang dikirim maupun tidak, dicatat ke `sinyal_log.csv`. Setelah 7 hari, bot mengisi kolom `hasil`: TARGET 1, CUT LOSS, atau TIDAK KEDUANYA.
6. Tiap 7 hari bot mengirim laporan mingguan, sekaligus tanda bahwa bot masih hidup.

## Rincian skor conviction (maks 100)

| Komponen | Poin |
|---|---|
| Ada order beli whale | 15 |
| 2 order whale / 3 order atau lebih | +5 / +10 |
| Beli whale ≥ 2% / ≥ 5% dari volume 24 jam | +5 / +10 |
| Harga 24 jam ≤ 1,5% / ≤ 3% | +10 / +5 |
| Harga di atas rata rata 200 hari (atau EMA 50 hari untuk koin baru) | +10 |
| EMA 20 di atas EMA 50, grafik 6 jam | +8 |
| Higher low dalam 3 hari | +8 |
| RSI 14 hari antara 40 dan 65 | +7 |
| Harga maks 8% di atas dasar kisaran 30 hari | +7 |
| Kisaran 30 hari ≤ 25% (pola akumulasi) | +5 |
| Volume 24 jam ≥ 1,2x rata rata 20 hari | +5 |
| BTC di atas EMA 50 harian | +10 |
| **Pengurang:** RSI di atas 75 | −10 |
| **Pengurang:** BTC di bawah rata rata 200 hari | −10 |
| **Pengurang:** jual whale ≥ 50% dari beli whale | −10 |
| **Pengurang:** rasio untung/rugi di bawah 1:1,5 | −10 |
| **Pemblokir:** jual whale ≥ beli whale | sinyal tidak dikirim |

Skor ini adalah skor aturan, **bukan peluang menang**. Peluang menang yang sebenarnya dibaca dari kolom `hasil` di `sinyal_log.csv` setelah sinyal terkumpul.

## Mengubah pengaturan tanpa menyentuh kode

Di repositori GitHub: **Settings → Secrets and variables → Actions → tab Variables → New repository variable**.

| Nama variabel | Bawaan | Arti |
|---|---|---|
| `WHALE_MIN_USD` | 300000 | Nilai minimal satu order beli whale (USD) |
| `MAX_PRICE_CHANGE_24H` | 3 | Batas "harga belum bergerak" dalam persen |
| `MIN_CONVICTION` | 50 | Skor minimal untuk mengirim sinyal |

## Batasan yang perlu diketahui

- Data whale hanya dari Coinbase. Whale yang membeli di bursa lain atau lewat OTC tidak terlihat.
- "Satu order" adalah perkiraan: potongan yang terjadi di detik dan sisi yang sama dianggap satu order. Whale yang memecah order di waktu berbeda tidak akan terhitung sebagai satu order $300.000.
- Jadwal GitHub kadang molor beberapa menit. Tidak ada transaksi yang terlewat karena bot melanjutkan dari transaksi terakhir yang sudah dibaca.
- GitHub bisa menonaktifkan jadwal di repositori yang lama tidak ada aktivitas. Kalau laporan mingguan tidak datang, buka tab **Actions** dan aktifkan kembali.
- Sinyal adalah bahan pertimbangan, bukan saran keuangan.
