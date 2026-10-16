// 视频房间服务器：提供网页、PeerJS 信令（/peerjs），以及 TURN 中转的临时密码（/config.json）。
// 环境变量：
//   PORT          监听端口，默认 9000（前面用 Caddy/Nginx 做 https 反向代理）
//   TURN_HOST     TURN 服务器地址（域名或 IP），不填就不提供中转
//   TURN_SECRET   和 coturn 配置里 static-auth-secret 一致
const http = require('http');
const path = require('path');
const crypto = require('crypto');
const express = require('express');
const { ExpressPeerServer } = require('peer');

const PORT = Number(process.env.PORT) || 9000;
const TURN_HOST = process.env.TURN_HOST || '';
const TURN_SECRET = process.env.TURN_SECRET || '';
const WEB_DIR = path.join(__dirname, '..');

const app = express();
const server = http.createServer(app);

app.set('trust proxy', true);
app.disable('x-powered-by');

// coturn 的 REST 临时密码：用户名带过期时间，密码 = HMAC-SHA1(secret, 用户名)
function turnCredentials() {
  const username = Math.floor(Date.now() / 1000) + 12 * 3600 + ':vc';
  const credential = crypto.createHmac('sha1', TURN_SECRET).update(username).digest('base64');
  return { username, credential };
}

app.get('/config.json', (req, res) => {
  const iceServers = [];
  if (TURN_HOST) iceServers.push({ urls: `stun:${TURN_HOST}:3478` });
  if (TURN_HOST && TURN_SECRET) {
    iceServers.push({
      urls: [`turn:${TURN_HOST}:3478?transport=udp`, `turn:${TURN_HOST}:3478?transport=tcp`],
      ...turnCredentials(),
    });
  }
  iceServers.push({ urls: 'stun:stun.miwifi.com:3478' });
  res.set('Cache-Control', 'no-store');
  res.json({ selfHosted: true, path: '/peerjs', iceServers });
});

app.use('/peerjs', ExpressPeerServer(server, { path: '/', proxied: true, allow_discovery: false }));

// 只公开网页需要的两个文件
app.get('/', (req, res) => res.sendFile(path.join(WEB_DIR, 'index.html')));
app.get('/index.html', (req, res) => res.redirect(301, '/' + (req.url.split('?')[1] ? '?' + req.url.split('?')[1] : '')));
app.get('/peerjs.min.js', (req, res) => res.sendFile(path.join(WEB_DIR, 'peerjs.min.js'), { maxAge: '7d' }));

server.listen(PORT, () => {
  console.log(`视频房间已启动：http://localhost:${PORT}  中转：${TURN_HOST && TURN_SECRET ? TURN_HOST : '未配置'}`);
});
