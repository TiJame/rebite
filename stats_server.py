#!/usr/bin/env python3
"""
Servidor local do painel do Rebite.

- Serve o patio-do-rebite.html (e outros arquivos estáticos da mesma pasta).
- Expõe /api/stats com temperatura, CPU, RAM, disco, uptime e estado de energia do Raspberry Pi.

Uso:   python3 stats_server.py
Abrir: http://localhost:8000/patio-do-rebite.html

Só usa a biblioteca padrão do Python (nada para instalar).
Por padrão escuta apenas em 127.0.0.1 (somente o próprio Pi acessa).
Para acessar de outro computador da rede:  REBITE_HOST=0.0.0.0 python3 stats_server.py
Fora de um Linux (ex.: no Mac) devolve valores simulados, com "sim": true.
"""
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("REBITE_HOST", "127.0.0.1")
PORT = int(os.environ.get("REBITE_PORT", "8000"))
ROOT = os.path.dirname(os.path.abspath(__file__))
IS_LINUX = platform.system() == "Linux"
ALLOWED_EXT = {".html", ".css", ".js", ".png", ".svg", ".ico", ".json", ".woff2"}

STATS = {}
LOCK = threading.Lock()
MARKET = {"ts": 0, "data": {}}


def fetch_ibov():
    """Ibovespa pelo endpoint público de gráficos do Yahoo Finance (sem chave). Cache de 2 min."""
    if time.time() - MARKET["ts"] < 120 and MARKET["data"]:
        return MARKET["data"]
    try:
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EBVSP?range=1d&interval=15m",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) rebite-painel"})
        with urllib.request.urlopen(req, timeout=8) as r:
            meta = json.loads(r.read().decode())["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
        data = {"ibov": round(price), "pct": round((price / prev - 1) * 100, 2), "fonte": "yahoo"}
    except Exception as e:
        data = {"erro": str(e)[:120]}
    MARKET["ts"], MARKET["data"] = time.time(), data
    return data


def read_cpu_times():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def read_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        pass
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"], timeout=2).decode()
        return float(out.split("=")[1].split("'")[0])
    except Exception:
        return None


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0])  # kB
    total = info["MemTotal"] / 1024.0
    avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024.0
    used = total - avail
    return used, total, (used / total * 100.0 if total else 0.0)


def read_throttled():
    try:
        out = subprocess.check_output(["vcgencmd", "get_throttled"], timeout=2).decode()
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return 0


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # não envia nada; só descobre a interface de saída
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def sampler():
    """Atualiza STATS a cada 2 s."""
    sim = {"t": 48.0, "cpu": 20.0, "ram": 38.0}
    prev = read_cpu_times() if IS_LINUX else None
    thr, thr_at = 0, 0.0
    first = True
    while True:
        time.sleep(0.5 if first else 2)
        first = False
        try:
            if IS_LINUX:
                cur = read_cpu_times()
                d_idle, d_total = cur[0] - prev[0], cur[1] - prev[1]
                prev = cur
                cpu = (1 - d_idle / d_total) * 100.0 if d_total > 0 else 0.0
                used, total, pct = read_mem()
                if time.time() - thr_at > 6:
                    thr, thr_at = read_throttled(), time.time()
                du = shutil.disk_usage("/")
                with open("/proc/uptime") as f:
                    up = float(f.read().split()[0])
                data = {
                    "sim": False,
                    "temp": read_temp(),
                    "cpu": round(cpu, 1),
                    "ram_pct": round(pct, 1),
                    "ram_used_mb": round(used),
                    "ram_total_mb": round(total),
                    "disk_pct": round(du.used / du.total * 100.0, 1),
                    "uptime_s": int(up),
                    "load1": round(os.getloadavg()[0], 2),
                    "throttled": thr,
                }
            else:  # modo simulado (Mac/Windows)
                sim["t"] = min(62, max(42, sim["t"] + random.uniform(-1.2, 1.2)))
                sim["cpu"] = min(85, max(3, sim["cpu"] + random.uniform(-12, 12)))
                sim["ram"] = min(70, max(25, sim["ram"] + random.uniform(-1.5, 1.5)))
                data = {
                    "sim": True,
                    "temp": round(sim["t"], 1),
                    "cpu": round(sim["cpu"], 1),
                    "ram_pct": round(sim["ram"], 1),
                    "ram_used_mb": round(sim["ram"] / 100 * 3800),
                    "ram_total_mb": 3800,
                    "disk_pct": 42.0,
                    "uptime_s": int(time.time() % 86400),
                    "load1": round(sim["cpu"] / 25, 2),
                    "throttled": 0,
                }
            data["ip"] = get_ip()
            data["host"] = socket.gethostname()
            data["ts"] = int(time.time())
            with LOCK:
                STATS.clear()
                STATS.update(data)
        except Exception as e:  # nunca derruba o servidor por causa de uma leitura
            with LOCK:
                STATS["error"] = str(e)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, *args):  # silencioso
        pass

    def end_headers(self):
        if not getattr(self, "_api", False):
            self.send_header("Cache-Control", "no-cache")  # sempre pega a versão nova após git pull
        super().end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/stats":
            self._api = True
            with LOCK:
                body = json.dumps(STATS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/market":
            self._api = True
            body = json.dumps(fetch_ibov()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/patio-do-rebite.html" + (("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""))
            self.end_headers()
            return
        parts = [p for p in path.split("/") if p]
        if any(p.startswith(".") for p in parts) or os.path.splitext(path)[1].lower() not in ALLOWED_EXT:
            self.send_error(404)
            return
        super().do_GET()


def main():
    threading.Thread(target=sampler, daemon=True).start()
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        raise SystemExit(f"Não consegui abrir {HOST}:{PORT} ({e}). Já tem outro servidor rodando nessa porta?")
    print(f"Painel em http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}/patio-do-rebite.html")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
