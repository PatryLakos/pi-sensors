import os, io, json, time, shutil, subprocess, threading, datetime
import urllib.request, urllib.parse
from zoneinfo import ZoneInfo
import telebot
from gpiozero import MotionSensor
from smbus2 import SMBus
from bmp280 import BMP280
from bme280 import BME280
from prometheus_client import Gauge, Counter, start_http_server

TOKEN = os.environ["TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
bot = telebot.TeleBot(TOKEN)

# --- Metryki dla Prometheusa ---
ROOM_T = Gauge("room_temperature_celsius", "Temperatura w pokoju")
ROOM_H = Gauge("room_humidity_percent", "Wilgotnosc w pokoju")
ROOM_P = Gauge("room_pressure_hpa", "Cisnienie atmosferyczne")
SENSOR_UP = Gauge("room_sensor_up", "1 gdy odczyt czujnika sie udal")
CPU_T = Gauge("pi_cpu_temperature_celsius", "Temperatura procesora Pi")
MOTION_TOTAL = Counter("motion_events_total", "Liczba wykryc ruchu")
MOTION_ACTIVE = Gauge("motion_active", "1 gdy czujnik widzi ruch")

# --- Stan czujnika ruchu (zapamietywany w pliku) ---
STATE_FILE = "/home/patryk/.monitor_state"

def load_monitor():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() != "off"
    except FileNotFoundError:
        return True

def save_monitor(on):
    with open(STATE_FILE, "w") as f:
        f.write("on" if on else "off")

monitoring = load_monitor()

# --- Czujnik ruchu (HC-SR501 na GPIO17) ---
pir = MotionSensor(17)
last = 0

def on_motion():
    global last
    MOTION_TOTAL.inc()
    MOTION_ACTIVE.set(1)
    if not monitoring:
        return
    if time.time() - last > 60:   # max 1 powiadomienie na minute
        last = time.time()
        bot.send_message(CHAT_ID, "🚶 Wykryto ruch!")

def on_no_motion():
    MOTION_ACTIVE.set(0)

pir.when_motion = on_motion
pir.when_no_motion = on_no_motion

# --- Czujnik temperatury (BME280 lub BMP280 na I2C, adres 0x76) ---
i2c_lock = threading.Lock()   # zeby bot i metryki nie czytaly naraz

def read_climate():
    with i2c_lock:
        bus = SMBus(1)
        try:
            chip = bus.read_byte_data(0x76, 0xD0)   # 0x60 = BME280, 0x58 = BMP280
            if chip == 0x60:
                s = BME280(i2c_dev=bus)
                s.get_temperature()
                time.sleep(1)                        # pierwszy odczyt bywa zly
                return s.get_temperature(), s.get_pressure(), s.get_humidity()
            s = BMP280(i2c_dev=bus)
            s.get_temperature()
            time.sleep(0.2)
            return s.get_temperature(), s.get_pressure(), None
        finally:
            bus.close()

def read_cpu_temp():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000

# --- Petla zbierajaca metryki co 30 s ---
def metrics_loop():
    while True:
        try:
            t, p, h = read_climate()
            ROOM_T.set(t)
            ROOM_P.set(p)
            if h is not None:
                ROOM_H.set(h)
            SENSOR_UP.set(1)
        except Exception:
            SENSOR_UP.set(0)
        try:
            CPU_T.set(read_cpu_temp())
        except Exception:
            pass
        time.sleep(30)

# --- Stan Raspberry Pi ---
def status_text():
    cpu_temp = read_cpu_temp()

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            mem[key] = int(val.split()[0])
    ram_total = mem["MemTotal"] / 1024
    ram_used = ram_total - mem["MemAvailable"] / 1024

    disk = shutil.disk_usage("/")
    disk_used = disk.used / 1024**3
    disk_total = disk.total / 1024**3

    with open("/proc/uptime") as f:
        up = int(float(f.read().split()[0]))
    days, rest = divmod(up, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60

    load = os.getloadavg()[0]

    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True).stdout
        code = out.strip().split("=")[1]
        power = "✅ OK" if code == "0x0" else f"⚠️ problem ({code})"
    except Exception:
        power = "?"

    motion = "🔔 wlaczony" if monitoring else "🔕 wylaczony"

    return (
        f"🖥 Raspberry Pi\n"
        f"🌡 CPU: {cpu_temp:.1f} °C\n"
        f"⚙️ Obciazenie: {load:.2f}\n"
        f"🧠 RAM: {ram_used:.0f} / {ram_total:.0f} MB\n"
        f"💾 Karta: {disk_used:.1f} / {disk_total:.1f} GB\n"
        f"⏱ Uptime: {days}d {hours}h {minutes}min\n"
        f"🔌 Zasilanie: {power}\n"
        f"🚶 Czujnik ruchu: {motion}"
    )

# --- Komendy ---
HELP_TEXT = """📖 Wszystkie komendy

🌡 Pomiary
/temp – temperatura, wilgotność i ciśnienie teraz
/status – stan Raspberry Pi (CPU, RAM, karta, zasilanie)

📈 Wykresy
/wykres – ostatnie 7 dni
/wykres 1 – ostatnia doba
/wykres 30 – ostatni miesiąc (maksimum)

📋 Raporty
/raport – ostatnie 10 h (tak jak rano o 8:00)
/raport 24 – ostatnia doba
/raport 168 – ostatni tydzień
/tydzien – podsumowanie tygodnia z wykresem i porównaniem

🚶 Czujnik ruchu
/monitor_on – włącz powiadomienia o ruchu
/monitor_off – wyłącz powiadomienia o ruchu

⚙️ Inne
/ping – sprawdź, czy bot działa
/help – ta lista

☀️ Raport z nocy przychodzi sam codziennie o 8:00.
📊 Podsumowanie tygodnia – w każdą niedzielę o 20:00."""

@bot.message_handler(commands=["start", "help"])
def help_cmd(m):
    if m.chat.id == CHAT_ID:
        bot.reply_to(m, HELP_TEXT)

@bot.message_handler(commands=["ping"])
def ping(m):
    if m.chat.id == CHAT_ID:
        bot.reply_to(m, "✅ Działam! Wszystkie komendy: /help")

@bot.message_handler(commands=["monitor_off"])
def monitor_off(m):
    global monitoring
    if m.chat.id != CHAT_ID:
        return
    monitoring = False
    save_monitor(False)
    bot.reply_to(m, "🔕 Czujnik ruchu wylaczony.\nWlaczysz go komenda /monitor_on")

@bot.message_handler(commands=["monitor_on"])
def monitor_on(m):
    global monitoring
    if m.chat.id != CHAT_ID:
        return
    monitoring = True
    save_monitor(True)
    bot.reply_to(m, "🔔 Czujnik ruchu wlaczony.")

@bot.message_handler(commands=["status"])
def status(m):
    if m.chat.id != CHAT_ID:
        return
    try:
        bot.reply_to(m, status_text())
    except Exception as e:
        bot.reply_to(m, f"⚠️ Blad: {e}")

@bot.message_handler(commands=["temp"])
def temp(m):
    if m.chat.id != CHAT_ID:
        return
    try:
        t, p, h = read_climate()
        msg = f"🌡 {t:.1f} °C\n🧭 {p:.0f} hPa"
        if h is not None:
            msg += f"\n💧 {h:.0f} %"
        bot.reply_to(m, msg)
    except Exception as e:
        bot.reply_to(m, f"⚠️ Blad czujnika: {e}")
# --- Wykresy na Telegram (/wykres) ---
# (tytul, metryka, jednostka, min. margines skali, miejsca po przecinku, sensowny zakres)
CHARTS = [
    ("Temperatura w pokoju", "room_temperature_celsius", "°C", 0.2, 1, (-10, 50)),
    ("Wilgotność", "room_humidity_percent", "%", 1, 0, (0, 100)),
    ("Ciśnienie", "room_pressure_hpa", "hPa", 1, 0, (900, 1100)),
    ("Temperatura CPU", "pi_cpu_temperature_celsius", "°C", 0.5, 1, (0, 100)),
]

def prom_range(query, days):
    end = time.time()
    start = end - days * 86400
    step = max(60, int(days * 86400 / 500))   # ok. 500 punktow na wykres
    qs = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"http://localhost:9090/api/v1/query_range?{qs}", timeout=20) as r:
        res = json.load(r)["data"]["result"]
    if not res:
        return [], []
    vals = res[0]["values"]
    return ([datetime.datetime.fromtimestamp(float(t)) for t, _ in vals],
            [float(v) for _, v in vals])

def make_chart(days):
    # matplotlib ladujemy dopiero przy pierwszym wykresie, zeby oszczedzac RAM
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import MaxNLocator, AutoMinorLocator, ScalarFormatter

    fig, axes = plt.subplots(len(CHARTS), 1, figsize=(9, 12), sharex=True)
    for ax, (title, q, unit, step, dec, (lo, hi)) in zip(axes, CHARTS):
        x, y = prom_range(q, days)
        # odrzucamy bledne odczyty (np. 740 hPa), zeby nie psuly skali
        pts = [(a, b) for a, b in zip(x, y) if lo <= b <= hi]
        ax.grid(which="major", alpha=0.4)
        ax.grid(which="minor", alpha=0.15)
        if not pts:
            ax.set_title(f"{title} ({unit})", fontsize=10)
            ax.text(0.5, 0.5, "brak danych", ha="center", va="center", transform=ax.transAxes)
            continue
        x, y = zip(*pts)
        ax.plot(x, y, linewidth=1.3)
        ax.set_title(
            f"{title} ({unit})  –  teraz {y[-1]:.{dec}f}, "
            f"min {min(y):.{dec}f}, max {max(y):.{dec}f}, sr. {sum(y)/len(y):.{dec}f}",
            fontsize=10)
        # skala dopasowana do danych z malym marginesem
        margin = max(step / 2, (max(y) - min(y)) * 0.1)
        ax.set_ylim(min(y) - margin, max(y) + margin)
        # gesta, czytelna siatka dopasowana do zakresu danych (ok. 8 podzialek)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=8, steps=[1, 2, 2.5, 5, 10]))
        ax.yaxis.set_minor_locator(AutoMinorLocator(5))
        sf = ScalarFormatter(useOffset=False)
        sf.set_scientific(False)
        ax.yaxis.set_major_formatter(sf)
    fmt = "%H:%M" if days <= 1 else "%d.%m %H:%M"
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    axes[-1].xaxis.set_minor_locator(AutoMinorLocator(4))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf

@bot.message_handler(commands=["wykres"])
def wykres(m):
    if m.chat.id != CHAT_ID:
        return
    # /wykres -> 7 dni, /wykres 1 -> ostatnia doba, /wykres 30 -> miesiac
    parts = m.text.split()
    try:
        days = int(parts[1]) if len(parts) > 1 else 7
    except ValueError:
        bot.reply_to(m, "Uzycie: /wykres [liczba dni], np. /wykres 1")
        return
    days = max(1, min(days, 30))   # Prometheus trzyma 30 dni
    try:
        bot.send_chat_action(CHAT_ID, "upload_photo")
        img = make_chart(days)
        label = "ostatnia doba" if days == 1 else f"ostatnie {days} dni"
        bot.send_photo(CHAT_ID, img, caption=f"📈 Pomiary – {label}")
    except Exception as e:
        bot.reply_to(m, f"⚠️ Nie udalo sie zrobic wykresu: {e}")

# --- Poranny raport (codziennie o 8:00) ---
TZ = ZoneInfo("Europe/Warsaw")
REPORT_HOUR = 8
NIGHT_HOURS = 10   # raport obejmuje 22:00 -> 8:00

def prom_query(q):
    qs = urllib.parse.urlencode({"query": q})
    with urllib.request.urlopen(f"http://localhost:9090/api/v1/query?{qs}", timeout=20) as r:
        res = json.load(r)["data"]["result"]
    return float(res[0]["value"][1]) if res else None

def fmt(v, dec=1, unit=" °C"):
    return "brak danych" if v is None else f"{v:.{dec}f}{unit}"

def morning_report(hours=NIGHT_HOURS):
    w = f"[{hours}h:1m]"
    temp = "(room_temperature_celsius > -10 < 50)"   # bez blednych odczytow
    hum = "(room_humidity_percent >= 0 <= 100)"
    t_min = prom_query(f"min_over_time({temp}{w})")
    t_max = prom_query(f"max_over_time({temp}{w})")
    t_now = prom_query(f"last_over_time({temp}[5m])")
    h_avg = prom_query(f"avg_over_time({hum}{w})")
    h_max = prom_query(f"max_over_time({hum}{w})")
    moves = prom_query(f"sum(increase(motion_events_total[{hours}h]))")
    moves = round(moves) if moves is not None else None

    start = (datetime.datetime.now(TZ) - datetime.timedelta(hours=hours)).strftime("%H:%M")
    lines = [
        "☀️ Dzien dobry!" if hours == NIGHT_HOURS else "📋 Raport",
        f"Ostatnie {hours} h (od {start})",
        "",
        f"🌡 Temperatura: min {fmt(t_min)}, max {fmt(t_max)}",
        f"     teraz {fmt(t_now)}",
        f"💧 Wilgotnosc: srednio {fmt(h_avg, 0, ' %')}, max {fmt(h_max, 0, ' %')}",
    ]
    if moves is None:
        lines.append("🚶 Ruch: brak danych")
    elif moves == 0:
        lines.append("🚶 Ruch: zero, spokojna noc 😴")
    else:
        lines.append(f"🚶 Ruch w pokoju: {moves} razy")
    if h_max is not None and h_max > 60:
        lines.append("\n💡 Wysoka wilgotnosc - przewietrz pokoj.")
    if t_min is not None and t_min < 17:
        lines.append("\n🥶 W nocy bylo zimno - sprawdz okno albo grzejnik.")
    return "\n".join(lines)

# --- Tygodniowe podsumowanie (niedziela 20:00) ---
WEEKLY_DAY = 6      # 0 = poniedzialek ... 6 = niedziela
WEEKLY_HOUR = 20

def week_stat(func, metric_filter, ago=False):
    off = " offset 7d" if ago else ""
    return prom_query(f"{func}({metric_filter}[7d:5m]{off})")

def compare(now, prev, unit, dec, more, less):
    if now is None or prev is None:
        return ""
    d = now - prev
    if round(d, dec) == 0:
        return " (tak samo jak tydzien temu)"
    word = more if d > 0 else less
    return f" (o {abs(d):.{dec}f}{unit} {word} niz tydzien temu)"

def weekly_summary():
    temp = "(room_temperature_celsius > -10 < 50)"
    hum = "(room_humidity_percent >= 0 <= 100)"
    t_avg, t_avg_prev = week_stat("avg_over_time", temp), week_stat("avg_over_time", temp, ago=True)
    t_min, t_max = week_stat("min_over_time", temp), week_stat("max_over_time", temp)
    h_avg, h_avg_prev = week_stat("avg_over_time", hum), week_stat("avg_over_time", hum, ago=True)
    moves = prom_query("sum(increase(motion_events_total[7d]))")
    moves_prev = prom_query("sum(increase(motion_events_total[7d] offset 7d))")

    lines = ["📊 Podsumowanie tygodnia", ""]
    lines.append(f"🌡 Temperatura: srednio {fmt(t_avg)}"
                 + compare(t_avg, t_avg_prev, " °C", 1, "cieplej", "chlodniej"))
    lines.append(f"     min {fmt(t_min)}, max {fmt(t_max)}")
    lines.append(f"💧 Wilgotnosc: srednio {fmt(h_avg, 0, ' %')}"
                 + compare(h_avg, h_avg_prev, " pkt %", 0, "wyzej", "nizej"))

    if moves is None:
        lines.append("🚶 Ruch: brak danych")
    else:
        m_now = round(moves)
        line = f"🚶 Ruch w pokoju: {m_now} razy"
        if moves_prev is not None:
            m_prev = round(moves_prev)
            if m_prev > 0:
                pct = (m_now - m_prev) / m_prev * 100
                if abs(pct) < 5:
                    line += " (podobnie jak tydzien temu)"
                else:
                    word = "czesciej" if pct > 0 else "rzadziej"
                    line += f" (o {abs(pct):.0f}% {word} niz tydzien temu)"
            else:
                line += f" (tydzien temu: {m_prev})"
        lines.append(line)

    if t_avg_prev is None:
        lines.append("\nℹ️ Porownanie pojawi sie, gdy zbierze sie wiecej niz tydzien danych.")
    return "\n".join(lines)

def send_weekly():
    text = weekly_summary()
    bot.send_chat_action(CHAT_ID, "upload_photo")
    img = make_chart(7)
    bot.send_photo(CHAT_ID, img, caption=text)

def report_loop():
    # co 30 s sprawdzamy godzine; odporne na restarty i synchronizacje zegara
    last_daily = None
    last_weekly = None
    while True:
        now = datetime.datetime.now(TZ)
        if now.hour == REPORT_HOUR and now.minute < 10 and last_daily != now.date():
            last_daily = now.date()
            try:
                bot.send_message(CHAT_ID, morning_report())
            except Exception as e:
                print(f"Blad raportu: {e}", flush=True)
        if (now.weekday() == WEEKLY_DAY and now.hour == WEEKLY_HOUR
                and now.minute < 10 and last_weekly != now.date()):
            last_weekly = now.date()
            try:
                send_weekly()
            except Exception as e:
                print(f"Blad podsumowania: {e}", flush=True)
        time.sleep(30)

@bot.message_handler(commands=["raport"])
def raport(m):
    if m.chat.id != CHAT_ID:
        return
    # /raport -> ostatnie 10 h, /raport 24 -> ostatnia doba
    parts = m.text.split()
    try:
        hours = int(parts[1]) if len(parts) > 1 else NIGHT_HOURS
    except ValueError:
        bot.reply_to(m, "Uzycie: /raport [liczba godzin], np. /raport 24")
        return
    hours = max(1, min(hours, 24 * 30))
    try:
        bot.reply_to(m, morning_report(hours))
    except Exception as e:
        bot.reply_to(m, f"⚠️ Nie udalo sie zrobic raportu: {e}")

@bot.message_handler(commands=["tydzien"])
def tydzien(m):
    if m.chat.id != CHAT_ID:
        return
    try:
        send_weekly()
    except Exception as e:
        bot.reply_to(m, f"⚠️ Nie udalo sie zrobic podsumowania: {e}")

# --- Start ---
start_http_server(8000, addr="127.0.0.1")   # metryki pod http://localhost:8000/metrics
threading.Thread(target=metrics_loop, daemon=True).start()
threading.Thread(target=report_loop, daemon=True).start()

bot.send_message(CHAT_ID, "🟢 Bot uruchomiony")
bot.infinity_polling()