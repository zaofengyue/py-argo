# nodex-argo-py

基于 xray + Cloudflare Argo 隧道的多协议代理工具，支持 VMess / VLESS / Trojan 三协议，支持临时隧道和固定隧道。

## 目录结构

```
app.py      全部逻辑：配置加载、xray/cloudflared 下载、xray 配置与启动、
            Argo 隧道、分享链接与订阅生成、转发服务、主入口
Dockerfile  Docker 部署用
```


## 部署方式

### 方式一：Docker

```bash
docker build -t nodex-argo-py .
docker run -d \
  -e ARGO_DOMAIN=你的域名 \
  -e ARGO_AUTH=你的Token \
  -p 3000:3000 \
  -e PORT=3000 \
  nodex-argo-py
```


### 方式二：源码文件上传部署

将 `app.py` 上传到目标平台，确保运行环境有 Python 3.9+ 且能访问 GitHub（用于首次运行时下载 xray/cloudflared），然后：

```bash
python app.py
```

## 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `UUID` | VMess/VLESS/Trojan 统一 ID（Trojan 密码直接复用此值） | 自动生成并持久化 |
| `PORT` | 对外监听端口 | 自动分配空闲端口 |
| `ARGO_PORT` | Argo 内部转发端口 | 固定隧道默认 8001，临时隧道自动分配 |
| `NAME` | 节点名称前缀 | `xray-node` |
| `SUB` | 订阅路径 | `sub` |
| `ARGO_DOMAIN` | 固定隧道域名 | 留空则用临时隧道 |
| `ARGO_AUTH` | 固定隧道 Token | 留空则用临时隧道 |

也可以不设环境变量，直接改 `app.py` 开头的 `CONF_*` 常量，优先级高于环境变量。

## 数据文件位置

运行时数据默认存放在 `~/nodex-argo-py/`：
- `uuid.txt`：持久化的 UUID
- `xray-config.json`：生成的 xray 配置
- `xray/`、`cloudflared`：下载的二进制
- `sub.txt`：生成的订阅内容（base64）


## 注意事项

- xray、cloudflared 首次运行会自动下载，需要能访问 GitHub。
- 临时隧道域名每次重启会变化，重启后需要重新获取订阅。
- 固定隧道需要在 Cloudflare Zero Trust 中创建 Tunnel 并拿到 Token，参考 Cloudflare 官方文档。
- 仅供学习研究使用，部署前请确认符合所在平台和当地法律法规的要求。
