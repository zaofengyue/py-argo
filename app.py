"""app.py — py-argo 单文件版

基于 xray + Cloudflare Argo 隧道的多协议代理服务，单文件实现：
配置加载、xray/cloudflared 下载、xray 配置与启动、Argo 隧道建立、
分享链接与订阅生成、WS 转发服务、部署后临时文件清理、主入口。
"""
import base64
import json
import logging
import os
import platform
import re
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

# ============================================================================
# 配置区（可在这里直接填写，优先级高于环境变量；留空则读环境变量，都没有则自动生成/使用默认值）
# ============================================================================
CONF_UUID = ""          # VMess/VLESS 统一 ID
CONF_TROJAN_PASS = ""   # Trojan 独立密码（留空则自动生成并持久化）
CONF_PORT = ""          # 对外监听端口
CONF_ARGO_PORT = ""     # Argo 内部转发端口（固定隧道默认 8001）
CONF_NAME = ""          # 节点名称前缀（留空则自动识别 国家-ASN）
CONF_SUB = ""           # 订阅路径，默认 sub
CONF_ARGO_DOMAIN = ""   # 固定隧道域名（留空用临时隧道）
CONF_ARGO_AUTH = ""     # 固定隧道 Token（留空用临时隧道）
CONF_CF_PREFER_HOST = "cdns.doon.eu.org"  # 分享链接中使用的 CDN 前置域名
CONF_CLEANUP_AFTER_DEPLOY = True  # 部署成功后自动清理不再需要的临时文件（见 cleanup_deploy_artifacts）
# ============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("py-argo")

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
HOME = Path(os.environ.get("HOME", "/tmp"))
APP_DIR = HOME / "py-argo"
UUID_FILE = APP_DIR / "uuid.txt"
TROJAN_FILE = APP_DIR / "trojan.txt"
XRAY_CONFIG_FILE = APP_DIR / "xray-config.json"
XRAY_DIR = APP_DIR / "xray"
XRAY_BIN_PATH = XRAY_DIR / "xray"
CLOUDFLARED_BIN = APP_DIR / "cloudflared"
SUB_FILE = APP_DIR / "sub.txt"
INDEX_HTML_FILE = Path.cwd() / "index.html"

WS_PATH_VMESS = "/py-argo-vm"
WS_PATH_VLESS = "/py-argo-vl"
WS_PATH_TROJAN = "/py-argo-tr"

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

FALLBACK_STATUS_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>py-argo</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto;line-height:1.6">
<h1>py-argo</h1>
<p>This host is running a personal xray + Cloudflare Argo tunnel proxy service.</p>
<p>Subscription endpoint: <code>{sub_path}</code></p>
</body></html>
"""


# ---------------------------------------------------------------------------
# 配置加载
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


def _http_get_text(url: str, timeout: int = 5) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode().strip()
    except Exception:
        return ""


def detect_node_name() -> str:
    """自动识别节点名：国家代码 + ASN 运营商，与 Node 版行为对齐。"""
    country = _http_get_text("https://ipinfo.io/country") or _http_get_text("https://ifconfig.co/country-iso")

    asn_org = _http_get_text("https://ipinfo.io/org") or _http_get_text("https://ifconfig.co/org")
    if asn_org:
        asn_org = re.sub(r"^AS\d+\s+", "", asn_org)
        asn_org = re.sub(r",?\s*Inc\.?$", "", asn_org)
        asn_org = re.sub(r",?\s*LLC\.?", "", asn_org)
        asn_org = re.sub(r",?\s*Ltd\.?", "", asn_org)
        asn_org = re.sub(r",?\s*Corp\.?", "", asn_org)
        asn_org = asn_org.strip()[:20]

    if country and asn_org:
        return f"{country}-{asn_org}"
    if country:
        return f"{country}-xray"
    return "xray"


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
    cleanup_after_deploy: bool = True

    @property
    def use_fixed_tunnel(self) -> bool:
        return bool(self.argo_domain and self.argo_auth)


def load_settings() -> Settings:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    # UUID：配置区 > 环境变量 > 本地持久化文件 > 自动生成
    env_uuid = CONF_UUID or os.environ.get("UUID", "")
    node_uuid = env_uuid or _read_or_create(UUID_FILE, lambda: str(uuid.uuid4()))
    if env_uuid:
        UUID_FILE.write_text(env_uuid)

    # Trojan 密码：独立于 UUID，配置区 > 环境变量 > 本地持久化文件 > 自动生成
    env_trojan = CONF_TROJAN_PASS or os.environ.get("TROJAN_PASS", "")
    trojan_pass = env_trojan or _read_or_create(TROJAN_FILE, lambda: os.urandom(16).hex())
    if env_trojan:
        TROJAN_FILE.write_text(env_trojan)

    port_env = CONF_PORT or os.environ.get("PORT", "")
    inbound_port = int(port_env) if port_env else get_free_port()

    argo_domain = CONF_ARGO_DOMAIN or os.environ.get("ARGO_DOMAIN", "")
    argo_auth = CONF_ARGO_AUTH or os.environ.get("ARGO_AUTH", "")

    argo_port_env = CONF_ARGO_PORT or os.environ.get("ARGO_PORT", "")
    if argo_domain and argo_auth:
        argo_port = int(argo_port_env) if argo_port_env else 8001
    else:
        argo_port = int(argo_port_env) if argo_port_env else get_free_port()

    sub_raw = CONF_SUB or os.environ.get("SUB", "sub")
    sub_path = "/" + sub_raw.lstrip("/")

    name = CONF_NAME or os.environ.get("NAME", "")
    cf_prefer_host = CONF_CF_PREFER_HOST or os.environ.get("CF_PREFER_HOST", "")

    cleanup_env = os.environ.get("CLEANUP_AFTER_DEPLOY", "")
    cleanup_after_deploy = (
        cleanup_env.strip().lower() not in ("0", "false", "no")
        if cleanup_env else CONF_CLEANUP_AFTER_DEPLOY
    )

    return Settings(
        uuid=node_uuid, trojan_pass=trojan_pass, inbound_port=inbound_port,
        argo_port=argo_port, sub_path=sub_path, argo_domain=argo_domain,
        argo_auth=argo_auth, name=name, cf_prefer_host=cf_prefer_host,
        cleanup_after_deploy=cleanup_after_deploy,
    )


# ---------------------------------------------------------------------------
# 二进制下载
# ---------------------------------------------------------------------------
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
# 部署后清理
# ---------------------------------------------------------------------------
def cleanup_deploy_artifacts():
    """清理部署完成后不再需要的临时/附带文件，减少数据目录体积。

    只清理明确用不到的内容，不会碰持久化文件（uuid.txt / trojan.txt）、
    运行时必需文件（xray-config.json）或结果文件（sub.txt）：
      - 残留的下载压缩包（正常流程 ensure_xray 已经删过，这里做兜底）
      - xray 官方发行包里附带的 geoip.dat / geosite.dat / LICENSE / README 等，
        当前配置的出站是 freedom 且没有路由规则，这些 geo 数据文件用不到
    """
    removed = []

    zip_path = APP_DIR / "xray.zip"
    if zip_path.exists():
        try:
            zip_path.unlink()
            removed.append(zip_path.name)
        except OSError as e:
            log.debug("cleanup: failed to remove %s: %s", zip_path, e)

    unused_names = ("geoip.dat", "geosite.dat", "LICENSE", "LICENSE.txt", "README.md", "README.zh-CN.md")
    if XRAY_DIR.exists():
        for name in unused_names:
            p = XRAY_DIR / name
            if p.exists() and p != XRAY_BIN_PATH:
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError as e:
                    log.debug("cleanup: failed to remove %s: %s", p, e)

    if removed:
        log.info("cleanup: removed unused file(s): %s", ", ".join(removed))
    else:
        log.info("cleanup: nothing to remove")


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


def _stream_xray_logs(proc: subprocess.Popen):
    """把 xray 的 stdout/stderr 转发到本进程日志，避免被 DEVNULL 吞掉。"""
    def _pump(pipe, level):
        for line in iter(pipe.readline, ""):
            line = line.rstrip()
            if line:
                log.log(level, "[xray] %s", line)
        pipe.close()

    if proc.stdout:
        threading.Thread(target=_pump, args=(proc.stdout, logging.INFO), daemon=True).start()
    if proc.stderr:
        threading.Thread(target=_pump, args=(proc.stderr, logging.WARNING), daemon=True).start()


def start_xray(settings: Settings) -> subprocess.Popen:
    cfg = build_xray_config(settings)
    XRAY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    XRAY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    xray_bin = find_system_xray() or str(ensure_xray())
    log.info("starting xray: %s run -config %s", xray_bin, XRAY_CONFIG_FILE)
    proc = subprocess.Popen(
        [xray_bin, "run", "-config", str(XRAY_CONFIG_FILE)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    _stream_xray_logs(proc)
    return proc


def watch_xray(proc: subprocess.Popen, on_exit):
    """存活监控：xray 意外退出时触发回调（记录错误并让主进程退出）。"""
    def _watch():
        code = proc.wait()
        log.error("xray process exited unexpectedly with code %s", code)
        on_exit(code)

    threading.Thread(target=_watch, daemon=True).start()


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
    # 分享链接的连接地址优先用 CDN 前置域名，SNI/Host 仍是真实 Argo 域名
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


def _recv_headers(sock: socket.socket, max_size: int = 65536) -> bytes:
    """循环读取直到拿到完整请求头（\\r\\n\\r\\n），避免分片/长请求头解析失败。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_size:
            break
    return buf


def _handle_argo_connection(client_sock: socket.socket):
    try:
        client_sock.settimeout(10)
        buf = _recv_headers(client_sock)
        if b"\r\n\r\n" not in buf:
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


def _load_index_html(sub_path: str) -> str:
    if INDEX_HTML_FILE.exists():
        try:
            return INDEX_HTML_FILE.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("failed to read index.html: %s", e)
    return FALLBACK_STATUS_PAGE.format(sub_path=sub_path)


def run_public_http_server(settings: Settings, sub_content_holder: dict):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", settings.inbound_port))
    srv.listen(128)
    log.info("public http server listening on 0.0.0.0:%s", settings.inbound_port)

    index_html = _load_index_html(settings.sub_path)

    def handle(client_sock: socket.socket):
        try:
            client_sock.settimeout(5)
            buf = _recv_headers(client_sock)
            request_line = buf.split(b"\r\n", 1)[0].decode(errors="ignore")
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
                body = index_html.encode("utf-8")
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

    if not settings.name:
        log.info("auto-detecting node name...")
        settings.name = detect_node_name()

    log.info(
        "settings: inbound_port=%s argo_port=%s sub_path=%s fixed_tunnel=%s name=%s cf_prefer_host=%s",
        settings.inbound_port, settings.argo_port, settings.sub_path,
        settings.use_fixed_tunnel, settings.name, settings.cf_prefer_host,
    )

    xray_proc = start_xray(settings)
    log.info("xray started, pid=%s", xray_proc.pid)

    def _on_xray_exit(code):
        os._exit(1)

    watch_xray(xray_proc, _on_xray_exit)

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

    links_text = build_links(settings, host, settings.name)
    sub_b64 = build_subscription(links_text)
    sub_holder["content"] = sub_b64
    SUB_FILE.write_text(sub_b64)

    print("================= 订阅内容 =================")
    print(sub_b64)
    print("============================================")
    print(f"订阅地址: https://{host}{settings.sub_path}")
    print(f"节点文件: {SUB_FILE}")

    if settings.cleanup_after_deploy:
        cleanup_deploy_artifacts()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
        tunnel.stop()
        xray_proc.terminate()


if __name__ == "__main__":
    main()
