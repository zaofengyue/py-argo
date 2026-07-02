"""
app.py — nodex-argo-py 单文件版

功能：基于 xray + Cloudflare Argo 隧道的多协议代理服务（VMess/VLESS/Trojan），
支持临时隧道和固定隧道，提供订阅接口。

部署时只需上传这一个文件即可（配合 requirements 无第三方依赖）。
"""
import base64
import json
import logging
import os
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("nodex")

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
HOME = Path(os.environ.get("HOME", "/tmp"))
APP_DIR = HOME / "nodex-argo-py"
UUID_FILE = APP_DIR / "uuid.txt"
TROJAN_FILE = APP_DIR / "trojan.txt"
XRAY_CONFIG_FILE = APP_DIR / "xray-config.json"
XRAY_DIR = APP_DIR / "xray"
XRAY_BIN_PATH = XRAY_DIR / "xray"
CLOUDFLARED_BIN = APP_DIR / "cloudflared"
SUB_FILE = APP_DIR / "sub.txt"

WS_PATH_VMESS = "/nodex-vm"
WS_PATH_VLESS = "/nodex-vl"
WS_PATH_TROJAN = "/nodex-tr"

V_VMESS_PORT = 10000
V_VLESS_PORT = 10001
V_TROJAN_PORT = 10002

PATH_TO_PORT = {
    WS_PATH_VMESS: V_VMESS_PORT,
    WS_PATH_VLESS: V_VLESS_PORT,
    WS_PATH_TROJAN: V_TROJAN_PORT,
}

XRAY_ARCH_MAP = {
    "x86_64": "linux-64", "amd64": "linux-64",
    "aarch64": "linux-arm64-v8a", "arm64": "linux-arm64-v8a",
    "armv7l": "linux-arm32-v7a",
}
CLOUDFLARED_ARCH_MAP = {
    "x86_64": "linux-amd64", "amd64": "linux-amd64",
    "aarch64": "linux-arm64", "arm64": "linux-arm64",
    "armv7l": "linux-arm",
}

TRYCLOUDFLARE_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")

STATUS_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>nodex-argo-py</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto;line-height:1.6">
<h1>nodex-argo-py</h1>
<p>This host is running a personal xray + Cloudflare Argo tunnel proxy service.</p>
<p>Subscription endpoint: <code>{sub_path}</code></p>
</body></html>
"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_or_create(path: Path, generator) -> str:
    if path.exists():
        return path.read_text().strip()
    value = generator()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return value


@dataclass
class Settings:
    uuid: str
    trojan_pass: str
    inbound_port: int
    argo_port: int
    sub_path: str
    argo_domain: str
    argo_auth: str
    name: str = ""
    cf_prefer_host: str = field(default="")

    @property
    def use_fixed_tunnel(self) -> bool:
        return bool(self.argo_domain and self.argo_auth)


def load_settings() -> Settings:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    env_uuid = os.environ.get("UUID", "")
    node_uuid = env_uuid or _read_or_create(UUID_FILE, lambda: str(uuid.uuid4()))
    if env_uuid:
        UUID_FILE.write_text(env_uuid)

    env_trojan = os.environ.get("TROJAN_PASS", "")
    trojan_pass = env_trojan or _read_or_create(TROJAN_FILE, lambda: secrets.token_hex(16))
    if env_trojan:
        TROJAN_FILE.write_text(env_trojan)

    port_env = os.environ.get("PORT", "")
    inbound_port = int(port_env) if port_env else get_free_port()

    argo_domain = os.environ.get("ARGO_DOMAIN", "")
    argo_auth = os.environ.get("ARGO_AUTH", "")

    argo_port_env = os.environ.get("ARGO_PORT", "")
    if argo_domain and argo_auth:
        argo_port = int(argo_port_env) if argo_port_env else 8001
    else:
        argo_port = int(argo_port_env) if argo_port_env else get_free_port()

    sub_raw = os.environ.get("SUB", "sub")
    sub_path = "/" + sub_raw.lstrip("/")
    name = os.environ.get("NAME", "")

    return Settings(
        uuid=node_uuid, trojan_pass=trojan_pass, inbound_port=inbound_port,
        argo_port=argo_port, sub_path=sub_path, argo_domain=argo_domain,
        argo_auth=argo_auth, name=name,
    )


# ---------------------------------------------------------------------------
# 二进制下载
# ---------------------------------------------------------------------------
def _http_get_text(url: str, timeout: int = 5) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode().strip()
    except Exception:
        return ""


def ensure_xray() -> Path:
    if XRAY_BIN_PATH.exists():
        return XRAY_BIN_PATH

    plat = XRAY_ARCH_MAP.get(platform.machine(), "linux-64")
    log.info("downloading xray for %s", plat)

    version = "v25.4.30"
    release_json = _http_get_text("https://api.github.com/repos/XTLS/Xray-core/releases/latest")
    if release_json:
        try:
            version = json.loads(release_json).get("tag_name", version)
        except Exception:
            pass

    url = f"https://github.com/XTLS/Xray-core/releases/download/{version}/Xray-{plat}.zip"
    XRAY_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = APP_DIR / "xray.zip"
    urllib.request.urlretrieve(url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(XRAY_DIR)

    XRAY_BIN_PATH.chmod(XRAY_BIN_PATH.stat().st_mode | stat.S_IEXEC)
    zip_path.unlink(missing_ok=True)
    log.info("xray ready at %s", XRAY_BIN_PATH)
    return XRAY_BIN_PATH


def ensure_cloudflared() -> Path:
    if CLOUDFLARED_BIN.exists():
        CLOUDFLARED_BIN.chmod(CLOUDFLARED_BIN.stat().st_mode | stat.S_IEXEC)
        return CLOUDFLARED_BIN

    plat = CLOUDFLARED_ARCH_MAP.get(platform.machine(), "linux-amd64")
    log.info("downloading cloudflared for %s", plat)

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-{plat}"
    urllib.request.urlretrieve(url, CLOUDFLARED_BIN)
    CLOUDFLARED_BIN.chmod(CLOUDFLARED_BIN.stat().st_mode | stat.S_IEXEC)
    log.info("cloudflared ready at %s", CLOUDFLARED_BIN)
    return CLOUDFLARED_BIN


def find_system_xray() -> str:
    import shutil
    for candidate in ("xray", "/usr/local/bin/xray", "/usr/bin/xray"):
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if path:
            return path
    return ""


# ---------------------------------------------------------------------------
# xray 配置与启动
# ---------------------------------------------------------------------------
def build_xray_config(settings: Settings) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": V_VMESS_PORT, "listen": "127.0.0.1", "protocol": "vmess",
                "settings": {"clients": [{"id": settings.uuid, "alterId": 0}]},
                "streamSettings": {"network": "ws", "wsSettings": {"path": WS_PATH_VMESS}},
            },
            {
                "port": V_VLESS_PORT, "listen": "127.0.0.1", "protocol": "vless",
                "settings": {"clients": [{"id": settings.uuid, "flow": ""}], "decryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"path": WS_PATH_VLESS}},
            },
            {
                "port": V_TROJAN_PORT, "listen": "127.0.0.1", "protocol": "trojan",
                "settings": {"clients": [{"password": settings.trojan_pass}]},
                "streamSettings": {"network": "ws", "wsSettings": {"path": WS_PATH_TROJAN}},
            },
        ],
        "outbounds": [{"protocol": "freedom", "settings": {}}],
    }


def start_xray(settings: Settings) -> subprocess.Popen:
    cfg = build_xray_config(settings)
    XRAY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    XRAY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    xray_bin = find_system_xray() or str(ensure_xray())
    log.info("starting xray: %s run -config %s", xray_bin, XRAY_CONFIG_FILE)
    return subprocess.Popen(
        [xray_bin, "run", "-config", str(XRAY_CONFIG_FILE)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Argo 隧道
# ---------------------------------------------------------------------------
class ArgoTunnel:
    def __init__(self, cloudflared_bin: str, settings: Settings):
        self.cloudflared_bin = cloudflared_bin
        self.settings = settings
        self.proc: Optional[subprocess.Popen] = None
        self.host: str = ""
        self._ready = threading.Event()

    def start(self, timeout: int = 30) -> str:
        if self.settings.use_fixed_tunnel:
            self.host = self.settings.argo_domain
            args = [
                self.cloudflared_bin, "tunnel", "--edge-ip-version", "auto",
                "--no-autoupdate", "run", "--token", self.settings.argo_auth,
            ]
            log.info("starting fixed argo tunnel -> %s", self.host)
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            return self.host

        args = [
            self.cloudflared_bin, "tunnel", "--edge-ip-version", "auto",
            "--no-autoupdate", "--url", f"http://127.0.0.1:{self.settings.argo_port}",
        ]
        log.info("starting temporary argo tunnel")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def _reader():
            for line in self.proc.stderr:
                m = TRYCLOUDFLARE_RE.search(line)
                if m and not self.host:
                    self.host = m.group(1)
                    log.info("temporary tunnel host: %s", self.host)
                    self._ready.set()
                    break

        threading.Thread(target=_reader, daemon=True).start()
        if not self._ready.wait(timeout):
            log.warning("timed out waiting for temporary tunnel host")
        return self.host

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------------------
# 分享链接 / 订阅内容
# ---------------------------------------------------------------------------
def build_links(settings: Settings, host: str, name: str) -> str:
    front_domain = settings.cf_prefer_host or host

    vmess_obj = {
        "v": "2", "ps": name, "add": front_domain, "port": "443",
        "id": settings.uuid, "aid": "0", "scy": "auto", "net": "ws", "type": "none",
        "host": host, "path": WS_PATH_VMESS, "tls": "tls", "sni": host,
    }
    vmess_link = "vmess://" + base64.b64encode(json.dumps(vmess_obj).encode()).decode()

    vless_link = (
        f"vless://{settings.uuid}@{front_domain}:443"
        f"?encryption=none&security=tls&sni={host}&type=ws&host={host}"
        f"&path={urllib.parse.quote(WS_PATH_VLESS)}#{urllib.parse.quote(name)}"
    )

    trojan_link = (
        f"trojan://{settings.trojan_pass}@{front_domain}:443"
        f"?security=tls&sni={host}&type=ws&host={host}"
        f"&path={urllib.parse.quote(WS_PATH_TROJAN)}#{urllib.parse.quote(name)}"
    )

    return "\n".join([vmess_link, vless_link, trojan_link])


def build_subscription(links_text: str) -> str:
    return base64.b64encode(links_text.encode()).decode()


# ---------------------------------------------------------------------------
# HTTP / WebSocket 转发服务
# ---------------------------------------------------------------------------
def _pipe(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle_argo_connection(client_sock: socket.socket):
    try:
        client_sock.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client_sock.recv(4096)
            if not chunk:
                client_sock.close()
                return
            buf += chunk
            if len(buf) > 65536:
                client_sock.close()
                return

        header_part, _, rest = buf.partition(b"\r\n\r\n")
        request_line = header_part.split(b"\r\n", 1)[0].decode(errors="ignore")
        try:
            _, path, _ = request_line.split(" ", 2)
        except ValueError:
            client_sock.close()
            return
        path = path.split("?")[0]

        target_port = PATH_TO_PORT.get(path)
        if target_port is None:
            client_sock.close()
            return

        client_sock.settimeout(None)
        upstream = socket.create_connection(("127.0.0.1", target_port), timeout=5)
        upstream.sendall(header_part + b"\r\n\r\n" + rest)

        t1 = threading.Thread(target=_pipe, args=(client_sock, upstream), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(upstream, client_sock), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
        client_sock.close()
        upstream.close()
    except Exception as e:
        log.debug("argo connection error: %s", e)
        client_sock.close()


def run_argo_forward_server(settings: Settings):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", settings.argo_port))
    srv.listen(128)
    log.info("argo forward server listening on 127.0.0.1:%s", settings.argo_port)

    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle_argo_connection, args=(client,), daemon=True).start()


def run_public_http_server(settings: Settings, sub_content_holder: dict):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", settings.inbound_port))
    srv.listen(128)
    log.info("public http server listening on 0.0.0.0:%s", settings.inbound_port)

    def handle(client_sock: socket.socket):
        try:
            client_sock.settimeout(5)
            data = client_sock.recv(4096)
            request_line = data.split(b"\r\n", 1)[0].decode(errors="ignore")
            try:
                _, path, _ = request_line.split(" ", 2)
            except ValueError:
                path = "/"
            path = path.split("?")[0]

            if path == settings.sub_path:
                body = sub_content_holder.get("content", "").encode()
                headers = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()
            else:
                body = STATUS_PAGE.format(sub_path=settings.sub_path).encode()
                headers = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()

            client_sock.sendall(headers + body)
        except Exception as e:
            log.debug("public http error: %s", e)
        finally:
            client_sock.close()

    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    settings = load_settings()
    log.info(
        "settings: inbound_port=%s argo_port=%s sub_path=%s fixed_tunnel=%s",
        settings.inbound_port, settings.argo_port, settings.sub_path, settings.use_fixed_tunnel,
    )

    xray_proc = start_xray(settings)
    log.info("xray started, pid=%s", xray_proc.pid)

    sub_holder = {"content": ""}
    threading.Thread(target=run_argo_forward_server, args=(settings,), daemon=True).start()
    threading.Thread(target=run_public_http_server, args=(settings, sub_holder), daemon=True).start()

    cloudflared_bin = str(ensure_cloudflared())
    tunnel = ArgoTunnel(cloudflared_bin, settings)
    host = tunnel.start()

    if not host:
        log.error("failed to establish argo tunnel, exiting")
        xray_proc.terminate()
        sys.exit(1)

    name = settings.name or "xray-node"
    links_text = build_links(settings, host, name)
    sub_b64 = build_subscription(links_text)
    sub_holder["content"] = sub_b64
    SUB_FILE.write_text(sub_b64)

    print("================= 订阅内容 =================")
    print(sub_b64)
    print("============================================")
    print(f"订阅地址: https://{host}{settings.sub_path}")
    print(f"节点文件: {SUB_FILE}")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
        tunnel.stop()
        xray_proc.terminate()


if __name__ == "__main__":
    main()
