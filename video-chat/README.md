# 视频房间

两个人打开网页、输入同一个房间号，就能视频通话。支持前后摄像头切换、静音、关摄像头；一个房间最多两人。

- `index.html`、`peerjs.min.js`：网页本身（纯静态，直接放 GitHub Pages 也能用，但走的是国外的公共信令服务器）
- `server/`：自己部署的服务器（网页 + 信令 + TURN 中转），国内用推荐这个

## 部署到自己的服务器（国内用）

**1. 准备服务器和域名**

- 推荐**香港**的轻量服务器（阿里云 / 腾讯云都有，1 核 1G 就够），系统选 **Ubuntu 24.04**。
  香港服务器不用备案，国内访问也快。
- 如果用**大陆**服务器，域名必须先做 ICP 备案，否则 80/443 端口打不开。
- 买一个域名（或用已有域名的子域名），添加一条 A 记录指向服务器公网 IP，例如 `v.example.com`。
  摄像头必须在 https 下才能用，所以一定要有域名。

**2. 在云控制台的「安全组/防火墙」放行端口**

| 端口 | 协议 | 用途 |
|---|---|---|
| 80、443 | TCP | 网页（https 证书申请也要用 80） |
| 3478 | TCP + UDP | TURN 中转 |
| 49160-49200 | UDP | 中转的音视频数据 |

**3. 上传代码并运行部署脚本**

在服务器上（香港服务器能直接访问 GitHub）：

```bash
git clone --depth 1 --filter=blob:none --sparse --branch video-chat \
  https://github.com/zhonghaichao88-a11y/j-j-j-j-kline.git
cd j-j-j-j-kline && git sparse-checkout set video-chat
sudo bash video-chat/server/deploy.sh v.example.com   # 换成你的域名
```

（只会下载 `video-chat` 这个文件夹，不会下载 K 线数据。）
大陆服务器连 GitHub 慢的话，也可以在电脑上把 `video-chat` 文件夹用 scp 传上去，再运行 `deploy.sh`。

完成后打开 `https://你的域名` 就能用。

## 常用命令

```bash
systemctl status video-chat          # 看服务状态
journalctl -u video-chat -f          # 看日志
systemctl restart video-chat         # 更新代码后重启
```

## 本地试运行

```bash
cd server && npm install && npm start   # 打开 http://localhost:9000
```
