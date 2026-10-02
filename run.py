"""
Единый запуск HotelGenie: бэкенд + туннель + бот.  Запуск: python run.py

Устройство:
  * бэкенд (uvicorn) и бот работают постоянно и независимо друг от друга;
  * туннель даёт публичный HTTPS-адрес; при обрыве он поднимается заново,
    а новый адрес записывается в файл .tunnel_url — бот подхватывает его
    сам (обновляет кнопку-меню) и перезапускать его не нужно;
  * раз в 30 секунд печатается строка статуса.

Остановка: Ctrl+C или красный квадрат в PyCharm.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "8000"))
URL_FILE = ROOT / ".tunnel_url"
ENV = {**os.environ, "PYTHONUNBUFFERED": "1"}
procs: list[subprocess.Popen] = []


def log(msg: str) -> None:
    print(f"[run] {msg}", flush=True)


def find_tool(name: str) -> str | None:
    for candidate in (shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def http_ok(url: str, timeout: float = 5) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def cleanup_old() -> None:
    """Убивает хвосты от прошлых запусков (иначе у бота будет Conflict)."""
    for pattern in ("bot.bot", "uvicorn backend.main", "nokey@localhost.run",
                    "cloudflared tunnel --url", "ngrok http"):
        subprocess.run(["pkill", "-f", pattern], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    time.sleep(1)


# --- Бэкенд и бот ---------------------------------------------------------

def start_backend() -> subprocess.Popen:
    log(f"Запускаю бэкенд на http://127.0.0.1:{PORT}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, env=ENV,
    )
    procs.append(proc)
    for _ in range(60):
        if proc.poll() is not None:
            sys.exit("[run] Бэкенд завершился сразу после запуска. Смотрите ошибку выше.")
        if http_ok(f"http://127.0.0.1:{PORT}/api/cities"):
            log("Бэкенд готов.")
            return proc
        time.sleep(0.5)
    sys.exit("[run] Бэкенд не ответил за 30 секунд.")


def start_bot() -> subprocess.Popen:
    log("Запускаю бота…")
    proc = subprocess.Popen([sys.executable, "-m", "bot.bot"], cwd=ROOT, env=ENV)
    procs.append(proc)
    return proc


# --- Туннели --------------------------------------------------------------

def _wait_url(proc: subprocess.Popen, pattern: re.Pattern, ready_marker: str | None,
              show: tuple[str, ...], tag: str, timeout: int) -> str:
    """Читает вывод процесса туннеля, ждёт адрес (и маркер готовности)."""
    state = {"url": None, "ready": ready_marker is None}
    done = threading.Event()
    ended = threading.Event()

    def reader() -> None:
        assert proc.stdout is not None
        shown = 0
        for line in proc.stdout:
            clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line).strip()
            m = pattern.search(clean)
            if m and not state["url"]:
                state["url"] = m.group(0)
            if ready_marker and ready_marker in clean:
                state["ready"] = True
            if state["url"] and state["ready"]:
                done.set()
            if clean and shown < 20 and any(k in clean for k in show):
                print(f"[{tag}] {clean}", flush=True)
                shown += 1
        ended.set()

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + timeout
    while not done.is_set():
        if ended.is_set() or time.time() > deadline:
            stop(proc)
            raise RuntimeError(f"{tag}: не удалось получить адрес")
        time.sleep(0.3)
    return state["url"]


def tunnel_ssh() -> tuple[subprocess.Popen, str]:
    ssh = find_tool("ssh")
    if not ssh:
        raise RuntimeError("ssh не найден")
    log("Туннель: localhost.run (ssh, без регистрации)…")
    proc = subprocess.Popen(
        [ssh, "-tt", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
         "-o", "ExitOnForwardFailure=yes",
         "-R", f"80:localhost:{PORT}", "nokey@localhost.run"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=ENV,
    )
    procs.append(proc)
    url = _wait_url(proc, re.compile(r"https://[a-z0-9]+\.lhr\.life"), None,
                    ("lhr.life", "denied", "error", "could not"), "ssh", 40)
    return proc, url


def tunnel_pinggy() -> tuple[subprocess.Popen, str]:
    ssh = find_tool("ssh")
    if not ssh:
        raise RuntimeError("ssh не найден")
    log("Туннель: pinggy (ssh, порт 443, без регистрации)…")
    # pinggy при анонимном входе просит пароль (пустой). Отвечаем автоматически.
    askpass = Path(tempfile.gettempdir()) / "hotelgenie_askpass.sh"
    askpass.write_text("#!/bin/sh\necho\n", encoding="utf-8")
    askpass.chmod(0o700)
    env = {**ENV, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
           "DISPLAY": os.getenv("DISPLAY", ":0")}
    proc = subprocess.Popen(
        [ssh, "-p", "443", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "NumberOfPasswordPrompts=1",
         "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
         "-o", "ExitOnForwardFailure=yes",
         f"-R0:localhost:{PORT}", "a.pinggy.io"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env, start_new_session=True,
    )
    procs.append(proc)
    url = _wait_url(proc, re.compile(r"https://[a-z0-9.-]+\.(?:free\.pinggy\.net|pinggy-free\.link|free\.pinggy\.link)"), None,
                    ("pinggy", "denied", "error"), "pinggy", 40)
    return proc, url


def tunnel_cloudflared() -> tuple[subprocess.Popen, str]:
    cf = find_tool("cloudflared")
    if not cf:
        raise RuntimeError("cloudflared не найден")
    log("Туннель: cloudflared (http2)…")
    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://localhost:{PORT}", "--protocol", "http2",
         "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=ENV,
    )
    procs.append(proc)
    url = _wait_url(proc, re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
                    "Registered tunnel connection", (" ERR ", "Registered tunnel"),
                    "cloudflared", 60)
    return proc, url


def tunnel_ngrok() -> tuple[subprocess.Popen, str]:
    ngrok = find_tool("ngrok")
    if not ngrok:
        raise RuntimeError("ngrok не найден")
    domain = re.sub(r"^https?://", "", os.getenv("NGROK_DOMAIN", "").strip()).strip("/")
    cmd = [ngrok, "http", str(PORT), "--log=stdout"]
    if domain:
        cmd.insert(2, f"--url={domain}")
    log("Туннель: ngrok…")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV)
    procs.append(proc)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(40):
        if proc.poll() is not None:
            break
        try:
            with opener.open("http://127.0.0.1:4040/api/tunnels", timeout=2) as resp:
                for t in json.load(resp).get("tunnels", []):
                    if t.get("public_url", "").startswith("https://"):
                        return proc, t["public_url"]
        except Exception:
            pass
        time.sleep(1)
    stop(proc)
    raise RuntimeError("ngrok не поднялся (проверьте authtoken)")


def verify(url: str, seconds: int = 15) -> bool:
    """Проверяет, что адрес реально отвечает (запрос идёт через интернет)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if http_ok(f"{url}/api/cities", timeout=4):
            return True
        time.sleep(1)
    return False


def start_any_tunnel() -> tuple[subprocess.Popen, str]:
    """Пробует туннели по очереди и берёт первый, который реально отвечает."""
    errors: list[str] = []
    unverified: list[tuple[subprocess.Popen, str]] = []
    for fn in (tunnel_ngrok, tunnel_ssh, tunnel_pinggy, tunnel_cloudflared):
        try:
            proc, url = fn()
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        log(f"Проверяю, что {url} отвечает…")
        if verify(url):
            log("Адрес отвечает.")
            for p, _ in unverified:
                stop(p)
            return proc, url
        log("Адрес не отвечает — пробую другой туннель…")
        unverified.append((proc, url))
    if unverified:
        for p, _ in unverified[1:]:
            stop(p)
        log("Ни один адрес не подтвердился проверкой с этого компьютера "
            "(при VPN это бывает) — использую первый.")
        return unverified[0]
    raise RuntimeError("; ".join(errors) or "нет доступных туннелей")


def publish_url(url: str | None) -> None:
    if url:
        tmp = URL_FILE.with_suffix(".tmp")
        tmp.write_text(url, encoding="utf-8")
        tmp.replace(URL_FILE)
    elif URL_FILE.exists():
        URL_FILE.unlink()


# --- Главный цикл ---------------------------------------------------------

def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    cleanup_old()
    publish_url(None)
    backend = start_backend()
    bot = start_bot()

    tunnel: subprocess.Popen | None = None
    url: str | None = None
    ok_once, fails = False, 0
    next_tunnel_try = 0.0
    next_check = next_status = time.time() + 20
    next_bot_try = 0.0

    try:
        while True:
            now = time.time()
            if backend.poll() is not None:
                sys.exit("[run] Бэкенд остановился. Выхожу.")

            if bot.poll() is not None and now >= next_bot_try:
                log("Бот остановился — запускаю заново через 3 секунды…")
                next_bot_try = now + 3
                time.sleep(3)
                bot = start_bot()

            if (tunnel is None or tunnel.poll() is not None) and now >= next_tunnel_try:
                if tunnel is not None:
                    log("Туннель оборвался — поднимаю заново…")
                stop(tunnel)
                tunnel = url = None
                publish_url(None)
                try:
                    tunnel, url = start_any_tunnel()
                    publish_url(url)
                    ok_once, fails = False, 0
                    log(f"Туннель готов. Адрес Mini App: {url}")
                    log("Бот подхватит адрес сам. В Telegram: бот → /start → кнопка "
                        "«🏨 Открыть HotelGenie» (или кнопка-меню «HotelGenie»).")
                except RuntimeError as exc:
                    log(f"Туннель не поднялся: {exc}. Повтор через 10 секунд…")
                    next_tunnel_try = time.time() + 10

            if url and now >= next_check:
                next_check = now + 20
                if http_ok(f"{url}/api/cities", timeout=4):
                    ok_once, fails = True, 0
                else:
                    fails += 1
                    if ok_once and fails >= 3:
                        log("Адрес перестал отвечать — перезапускаю туннель…")
                        stop(tunnel)

            if now >= next_status:
                next_status = now + 30
                log("статус: бэкенд {} | туннель {} | бот {} | {}".format(
                    "✔" if backend.poll() is None else "✘",
                    "✔" if tunnel and tunnel.poll() is None else "✘",
                    "✔" if bot.poll() is None else "✘",
                    url or "адреса нет"))
            time.sleep(2)
    except KeyboardInterrupt:
        log("Останавливаю…")
    finally:
        for p in procs:
            stop(p)
        publish_url(None)


if __name__ == "__main__":
    main()
