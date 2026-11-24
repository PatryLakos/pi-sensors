import os, time, shutil, subprocess, telebot
from gpiozero import MotionSensor
from smbus2 import SMBus
from bmp280 import BMP280

TOKEN = os.environ["TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
bot = telebot.TeleBot(TOKEN)

# --- Czujnik ruchu (HC-SR501 na GPIO17) ---
pir = MotionSensor(17)
last = 0

def on_motion():
    global last
    if time.time() - last > 60:   # max 1 powiadomienie na minute
        last = time.time()
        bot.send_message(CHAT_ID, "🚶 Wykryto ruch!")

pir.when_motion = on_motion

# --- Stan Raspberry Pi ---
def status_text():
    # temperatura procesora
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        cpu_temp = int(f.read()) / 1000

    # pamiec RAM
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            mem[key] = int(val.split()[0])
    ram_total = mem["MemTotal"] / 1024
    ram_used = ram_total - mem["MemAvailable"] / 1024

    # karta SD
    disk = shutil.disk_usage("/")
    disk_used = disk.used / 1024**3
    disk_total = disk.total / 1024**3

    # czas dzialania
    with open("/proc/uptime") as f:
        up = int(float(f.read().split()[0]))
    days, rest = divmod(up, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60

    # obciazenie CPU (srednia z 1 min)
    load = os.getloadavg()[0]

    # zasilanie - czy bylo za niskie napiecie
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True).stdout
        code = out.strip().split("=")[1]
        power = "✅ OK" if code == "0x0" else f"⚠️ problem ({code})"
    except Exception:
        power = "?"

    return (
        f"🖥 Raspberry Pi\n"
        f"🌡 CPU: {cpu_temp:.1f} °C\n"
        f"⚙️ Obciazenie: {load:.2f}\n"
        f"🧠 RAM: {ram_used:.0f} / {ram_total:.0f} MB\n"
        f"💾 Karta: {disk_used:.1f} / {disk_total:.1f} GB\n"
        f"⏱ Uptime: {days}d {hours}h {minutes}min\n"
        f"🔌 Zasilanie: {power}"
    )

# --- Komendy ---
@bot.message_handler(commands=["start", "ping"])
def ping(m):
    if m.chat.id == CHAT_ID:
        bot.reply_to(m, "✅ Dzialam! Komendy: /status /temp /ping")

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
        s = BMP280(i2c_dev=SMBus(1))
        s.get_temperature()
        time.sleep(0.2)
        t = s.get_temperature()
        p = s.get_pressure()
        bot.reply_to(m, f"🌡 {t:.1f} °C\n🧭 {p:.0f} hPa")
    except Exception as e:
        bot.reply_to(m, f"⚠️ Blad czujnika: {e}")

bot.send_message(CHAT_ID, "🟢 Bot uruchomiony")
bot.infinity_polling()
