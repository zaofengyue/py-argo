# py-argo

基于 xray + Cloudflare Argo 隧道的多协议代理工具，支持 VMess / VLESS / Trojan 三协议，支持临时隧道和固定隧道。

## 目录结构

```
app.py       全部逻辑：配置加载、xray/cloudflared 下载、xray 配置与启动、
             Argo 隧道、分享链接与订阅生成、转发服务、部署后清理、主入口
Dockerfile   Docker 部署用
index.html   可选，自定义落地页（与 app.py 同目录放置即可，不放则使用内置极简状态页）
```


## 部署方式

### 方式一：Docker

**直接拉取已构建好的镜像**（推荐，CI 已自动构建并推送到 GHCR）：

```bash
docker pull ghcr.io/zaofengyue/py-argo:latest
docker run -d \
  -e ARGO_DOMAIN=你的域名 \
  -e ARGO_AUTH=你的Token \
  -p 3000:3000 \
  -e PORT=3000 \
  ghcr.io/zaofengyue/py-argo:latest
```

**或者本地自行构建**：

```bash
docker build -t py-argo .
docker run -d \
  -e ARGO_DOMAIN=你的域名 \
  -e ARGO_AUTH=你的Token \
  -p 3000:3000 \
  -e PORT=3000 \
  py-argo
```


### 方式二：源码文件上传部署

将 `app.py` 上传到目标平台，确保运行环境有 Python 3.9+ 且能访问 GitHub（用于首次运行时下载 xray/cloudflared），然后：

```bash
python app.py
```

## 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `UUID` | VMess/VLESS 统一 ID | 自动生成并持久化 |
| `TROJAN_PASS` | Trojan 密码，与 `UUID` 相互独立 | 自动生成并持久化 |
| `PORT` | 对外监听端口 | 自动分配空闲端口 |
| `ARGO_PORT` | Argo 内部转发端口 | 固定隧道默认 8001，临时隧道自动分配 |
| `NAME` | 节点名称前缀 | 自动识别（国家代码-ASN 运营商，如 `US-Cloudflare`），识别失败则为 `xray` |
| `SUB` | 订阅路径 | `sub` |
| `ARGO_DOMAIN` | 固定隧道域名 | 留空则用临时隧道 |
| `ARGO_AUTH` | 固定隧道 Token | 留空则用临时隧道 |
| `CF_PREFER_HOST` | 分享链接中使用的 CDN 前置域名（连接地址），真实 Argo 域名仍作为 SNI/Host | `cdns.doon.eu.org` |
| `CLEANUP_AFTER_DEPLOY` | 部署成功、生成订阅后是否自动清理 xray 发行包里用不到的附带文件（geoip.dat/geosite.dat/LICENSE/README 等），设为 `0`/`false`/`no` 可关闭 | `true` |

也可以不设环境变量，直接改 `app.py` 开头的 `CONF_*` 常量，优先级高于环境变量。

## 数据文件位置

运行时数据默认存放在 `~/py-argo/`：
- `uuid.txt`：持久化的 UUID
- `trojan.txt`：持久化的 Trojan 密码（与 UUID 独立）
- `xray-config.json`：生成的 xray 配置
- `xray/`、`cloudflared`：下载的二进制
- `sub.txt`：生成的订阅内容（base64）


## 注意事项

- xray、cloudflared 首次运行会自动下载，需要能访问 GitHub。
- 临时隧道域名每次重启会变化，重启后需要重新获取订阅。
- 固定隧道需要在 Cloudflare Zero Trust 中创建 Tunnel 并拿到 Token，参考 Cloudflare 官方文档。
- xray 的运行日志会实时输出到控制台；xray 进程如果意外退出，主进程会记录错误并随之退出（便于部署平台自动重启，避免"服务在跑但代理已失效"的情况）。
- 部署成功、生成订阅后会自动清理 xray 发行包里用不到的附带文件（geoip.dat/geosite.dat/LICENSE 等），不会动 `uuid.txt`/`trojan.txt`/`xray-config.json`/`sub.txt`。如需保留这些文件排查问题，设置环境变量 `CLEANUP_AFTER_DEPLOY=false` 关闭。
- 如果 `app.py` 同目录下放了 `index.html`，会作为首页返回；不放则使用内置的极简状态页。
- 仅供学习研究使用，部署前请确认符合所在平台和当地法律法规的要求。
