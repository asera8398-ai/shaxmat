// ============================================================
// Shashka Arena — backend (Express + WebSocket)
// Bu server /public/index.html (frontend) ni beradi,
// /api/* so'rovlarga javob beradi va /ws orqali real vaqtli
// PvP (odam bilan o'yin) ni boshqaradi.
// ============================================================

require('dotenv').config();
const express = require('express');
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { WebSocketServer } = require('ws');

const BOT_TOKEN = process.env.BOT_TOKEN || '';
const BOT_USERNAME = process.env.BOT_USERNAME || '';
const PORT = process.env.PORT || 3000;
const INIT_DATA_MAX_AGE = Number(process.env.INIT_DATA_MAX_AGE || 86400);

if (!BOT_TOKEN) {
  console.warn('[OGOHLANTIRISH] BOT_TOKEN topilmadi (.env). initData tekshiruvi doim rad etiladi.');
}

// ------------------------------------------------------------
// 1) ODDIY FAYL-DB (MVP uchun). Katta loyihada bularni haqiqiy
//    bazaga (Postgres/Mongo) ko'chiring — bu yerda soddalik uchun
//    JSON faylga saqlanadi.
// ------------------------------------------------------------
const DB_PATH = path.join(__dirname, 'data', 'db.json');
let db = { users: {} };
try {
  db = JSON.parse(fs.readFileSync(DB_PATH, 'utf8'));
} catch (e) {
  fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
  saveDb();
}
let saveTimer = null;
function saveDb() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.writeFileSync(DB_PATH, JSON.stringify(db, null, 2));
  }, 250); // ko'p yozishlarni birlashtirish
}

function getUser(id, seed) {
  let u = db.users[id];
  if (!u) {
    u = db.users[id] = {
      id, name: seed?.name || "O'yinchi", photo: seed?.photo || '',
      points: 500, balance: 0, elo: 1200, w: 0, l: 0, d: 0,
      theme: 0, skin: 0, unl: { t: [0, 1, 2], s: [0, 1] }
    };
    saveDb();
  } else if (seed) {
    if (seed.name) u.name = seed.name;
    if (seed.photo) u.photo = seed.photo;
  }
  return u;
}

// ------------------------------------------------------------
// 2) TELEGRAM initData TEKSHIRUVI
//    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
// ------------------------------------------------------------
function verifyInitData(initData) {
  if (!initData || !BOT_TOKEN) return null;
  try {
    const params = new URLSearchParams(initData);
    const hash = params.get('hash');
    if (!hash) return null;
    params.delete('hash');
    const pairs = [];
    for (const [k, v] of params.entries()) pairs.push(`${k}=${v}`);
    pairs.sort();
    const dataCheckString = pairs.join('\n');

    const secretKey = crypto.createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
    const computedHash = crypto.createHmac('sha256', secretKey).update(dataCheckString).digest('hex');

    if (computedHash !== hash) return null;

    const authDate = Number(params.get('auth_date') || 0);
    if (INIT_DATA_MAX_AGE > 0 && Date.now() / 1000 - authDate > INIT_DATA_MAX_AGE) return null;

    const userRaw = params.get('user');
    const user = userRaw ? JSON.parse(userRaw) : null;
    if (!user || !user.id) return null;
    return user;
  } catch (e) {
    return null;
  }
}

// ------------------------------------------------------------
// 3) SERVER KONFIGURATSIYASI (frontend CFG bilan mos)
// ------------------------------------------------------------
const CFG = {
  prices: { undo: 40, king: 400, extra: 120, hint: 25 },
  rewards: [5, 15, 40],       // bot darajalari: oson/o'rta/qiyin
  themePrice: 300,
  skinPrice: 250,
  eloWin: 12,
  eloLose: 10,
  botUser: BOT_USERNAME,
  pvpReward: 20,
  pvpMinutes: 5
};

// ------------------------------------------------------------
// 4) EXPRESS
// ------------------------------------------------------------
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

function auth(req, res) {
  const tgUser = verifyInitData(req.body && req.body.initData);
  if (!tgUser) {
    res.json({ ok: false, error: 'auth' });
    return null;
  }
  const name = [tgUser.first_name, tgUser.last_name].filter(Boolean).join(' ') || "O'yinchi";
  return getUser(String(tgUser.id), { name, photo: tgUser.photo_url || '' });
}

app.post('/api/init', (req, res) => {
  const u = auth(req, res);
  if (!u) return;
  res.json({
    ok: true,
    user: {
      name: u.name, photo: u.photo, points: u.points, balance: u.balance,
      elo: u.elo, w: u.w, l: u.l, d: u.d, theme: u.theme, skin: u.skin,
      unl: u.unl, rank: 0
    },
    cfg: CFG,
    ann: [],
    tour: null,
    packs: [],
    top: topList()
  });
});

app.post('/api/game', (req, res) => {
  const u = auth(req, res);
  if (!u) return;
  const { result, level } = req.body || {};
  const lv = Math.max(0, Math.min(CFG.rewards.length - 1, Number(level) || 0));
  let reward = 0, eloDelta = 0;
  if (result === 'win') { reward = CFG.rewards[lv]; eloDelta = CFG.eloWin; u.w++; }
  else if (result === 'lose') { reward = 0; eloDelta = -CFG.eloLose; u.l++; }
  else { reward = Math.round(CFG.rewards[lv] / 3); eloDelta = 0; u.d++; }
  u.points += reward;
  u.elo = Math.max(100, u.elo + eloDelta);
  saveDb();
  res.json({ ok: true, points: u.points, elo: u.elo, reward, eloDelta, tour: null, limited: false });
});

app.post('/api/help', (req, res) => {
  const u = auth(req, res);
  if (!u) return;
  const kind = req.body && req.body.kind;
  const cost = CFG.prices[kind];
  if (!cost) return res.json({ ok: false });
  if (u.points < cost) return res.json({ ok: false, need: cost });
  u.points -= cost;
  saveDb();
  res.json({ ok: true, points: u.points });
});

app.post('/api/unlock', (req, res) => {
  const u = auth(req, res);
  if (!u) return;
  const { kind, index } = req.body || {};
  const cost = kind === 'theme' ? CFG.themePrice : CFG.skinPrice;
  const list = kind === 'theme' ? u.unl.t : u.unl.s;
  if (list.includes(index)) return res.json({ ok: true, points: u.points, unl: u.unl });
  if (u.points < cost) return res.json({ ok: false, need: cost });
  u.points -= cost;
  list.push(index);
  saveDb();
  res.json({ ok: true, points: u.points, unl: u.unl });
});

app.post('/api/profile', (req, res) => {
  const u = auth(req, res);
  if (!u) return;
  const { theme, skin } = req.body || {};
  if (theme !== undefined) u.theme = theme;
  if (skin !== undefined) u.skin = skin;
  saveDb();
  res.json({ ok: true });
});

function topList() {
  return Object.values(db.users)
    .sort((a, b) => b.elo - a.elo)
    .slice(0, 20)
    .map(u => ({ name: u.name, photo: u.photo, elo: u.elo, score: u.points, wins: u.w }));
}

app.post('/api/top', (req, res) => {
  const u = auth(req, res);
  if (!u) return;
  res.json({ ok: true, top: topList(), tour: null });
});

// ------------------------------------------------------------
// 5) WEBSOCKET — real vaqtli PvP
// ------------------------------------------------------------
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });

let queue = [];               // navbatda kutayotgan {uid, ws, name, elo, photo}
const sockets = new Map();    // uid -> ws (oxirgi ulanish)
const matches = new Map();    // matchId -> match

function send(ws, obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}

function opponentOf(match, uid) {
  return match.p1.uid === uid ? match.p2 : match.p1;
}

function endMatch(match, winnerUid, reason) {
  if (match.ended) return;
  match.ended = true;
  clearInterval(match.timer);
  matches.delete(match.id);

  [match.p1, match.p2].forEach(p => {
    const opp = p === match.p1 ? match.p2 : match.p1;
    const won = p.uid === winnerUid;
    const u = getUser(p.uid);
    const oppU = getUser(opp.uid);
    const expected = 1 / (1 + Math.pow(10, (oppU.elo - u.elo) / 400));
    const actual = winnerUid ? (won ? 1 : 0) : 0.5;
    const eloDelta = Math.round(24 * (actual - expected));
    u.elo = Math.max(100, u.elo + eloDelta);
    if (won) { u.w++; u.points += CFG.pvpReward; }
    else if (!winnerUid) { u.d++; }
    else { u.l++; }
    saveDb();
    send(p.ws, {
      type: 'ended', winner: won, reason,
      points: u.points, elo: u.elo,
      reward: won ? CFG.pvpReward : 0, eloDelta
    });
  });
}

function startClock(match) {
  match.timer = setInterval(() => {
    if (match.ended) return;
    const cur = match.turnUid;
    if (match.time[cur] != null) match.time[cur]--;
    send(match.p1.ws, { type: 'time', t: match.time });
    send(match.p2.ws, { type: 'time', t: match.time });
    if (match.time[cur] <= 0) {
      const other = match.p1.uid === cur ? match.p2.uid : match.p1.uid;
      endMatch(match, other, 'timeout');
    }
  }, 1000);
}

function tryMatch() {
  while (queue.length >= 2) {
    const a = queue.shift(), b = queue.shift();
    if (a.ws.readyState !== 1) { queue.unshift(b); continue; }
    if (b.ws.readyState !== 1) { queue.unshift(a); continue; }

    const matchId = crypto.randomUUID();
    const minutes = CFG.pvpMinutes;
    const time = { [a.uid]: minutes * 60, [b.uid]: minutes * 60 };
    const match = {
      id: matchId,
      p1: { uid: a.uid, ws: a.ws, name: a.name, elo: a.elo, photo: a.photo },
      p2: { uid: b.uid, ws: b.ws, name: b.name, elo: b.elo, photo: b.photo },
      turnUid: a.uid, // oq (birinchi) doim 'a'
      time, ended: false
    };
    matches.set(matchId, match);

    send(a.ws, { type: 'matched', matchId, minutes, color: 'w', opponent: { id: b.uid, name: b.name, elo: b.elo, photo: b.photo } });
    send(b.ws, { type: 'matched', matchId, minutes, color: 'b', opponent: { id: a.uid, name: a.name, elo: a.elo, photo: a.photo } });
    startClock(match);
  }
}

function findMatchByUid(uid) {
  for (const m of matches.values()) {
    if (!m.ended && (m.p1.uid === uid || m.p2.uid === uid)) return m;
  }
  return null;
}

wss.on('connection', (ws) => {
  ws.uid = null;
  const disconnectTimers = new Map();

  ws.on('message', (raw) => {
    let d;
    try { d = JSON.parse(raw); } catch (e) { return; }

    if (d.type === 'auth') {
      const tgUser = verifyInitData(d.initData);
      if (!tgUser) return send(ws, { type: 'invalid' });
      const uid = String(tgUser.id);
      ws.uid = uid;
      sockets.set(uid, ws);
      getUser(uid, { name: [tgUser.first_name, tgUser.last_name].filter(Boolean).join(' '), photo: tgUser.photo_url || '' });

      // agar shu foydalanuvchining tugallanmagan o'yini bo'lsa — qayta ulaymiz
      const existing = findMatchByUid(uid);
      if (existing) {
        const me = existing.p1.uid === uid ? existing.p1 : existing.p2;
        me.ws = ws;
        const opp = opponentOf(existing, uid);
        send(ws, {
          type: 'resync', matchId: existing.id, color: existing.p1.uid === uid ? 'w' : 'b',
          board: null, turn: existing.turnUid === uid ? 'me' : 'opp',
          t: existing.time, opponent: { id: opp.uid, name: opp.name, elo: opp.elo, photo: opp.photo }
        });
      }
      return send(ws, { type: 'ready', uid });
    }

    if (!ws.uid) return; // auth qilinmagan

    if (d.type === 'queue') {
      if (!queue.find(q => q.uid === ws.uid) && !findMatchByUid(ws.uid)) {
        const u = getUser(ws.uid);
        queue.push({ uid: ws.uid, ws, name: u.name, elo: u.elo, photo: u.photo });
        send(ws, { type: 'queued' });
        tryMatch();
      }
      return;
    }

    if (d.type === 'cancel') {
      queue = queue.filter(q => q.uid !== ws.uid);
      return;
    }

    if (d.type === 'move') {
      const m = findMatchByUid(ws.uid);
      if (!m || m.ended) return;
      if (m.turnUid !== ws.uid) return send(ws, { type: 'invalid' });
      const opp = opponentOf(m, ws.uid);
      m.turnUid = opp.uid; // oddiy alternativ navbat (ketma-ket urishni frontend o'zi boshqaradi)
      send(opp.ws, { type: 'opponent_move', move: { fr: d.fr, fc: d.fc, tr: d.tr, tc: d.tc, caps: d.caps, path: d.path, pk: d.pk } });
      return;
    }

    if (d.type === 'resign') {
      const m = findMatchByUid(ws.uid);
      if (!m || m.ended) return;
      const opp = opponentOf(m, ws.uid);
      endMatch(m, opp.uid, 'resign');
      return;
    }
  });

  ws.on('close', () => {
    queue = queue.filter(q => q.ws !== ws);
    if (!ws.uid) return;
    const m = findMatchByUid(ws.uid);
    if (!m || m.ended) return;
    // 20 soniya qayta ulanish uchun vaqt beramiz
    const t = setTimeout(() => {
      const still = findMatchByUid(ws.uid);
      if (still && !still.ended) {
        const opp = opponentOf(still, ws.uid);
        endMatch(still, opp.uid, 'disconnect');
      }
    }, 20000);
    disconnectTimers.set(ws.uid, t);
  });
});

server.listen(PORT, () => {
  console.log(`Shashka Arena backend ${PORT}-portda ishlamoqda`);
});
