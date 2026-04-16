const { Client, LocalAuth } = require('whatsapp-web.js');
const QRCode = require('qrcode');
const http = require('http');
const fs = require('fs');
const path = require('path');

const sessions = new Map();
const BRIDGE_HOST = process.env.WHATSAPP_BRIDGE_HOST || '127.0.0.1';
const BRIDGE_PORT = Number.parseInt(process.env.WHATSAPP_BRIDGE_PORT || '3000', 10);

// WA_SESSION_PATH lets you point session storage at a persistent volume
// (e.g. a Railway Volume mounted at /data/wa-sessions).
// If unset, whatsapp-web.js defaults to .wwebjs_auth/ in cwd — which is
// wiped on every container restart, causing the "always logs out" problem.
const WA_SESSION_PATH = process.env.WA_SESSION_PATH || null;

/**
 * Universal retry wrapper for Puppeteer/whatsapp-web.js operations.
 * Specifically handles 'detached Frame' and 'Execution context was destroyed' errors
 * by retrying the action, which usually triggers a frame re-attachment.
 */
async function withRetry(fn, maxRetries = 4) {
  let lastError;
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await fn();
    } catch (e) {
      lastError = e;
      const msg = e.message || '';
      if (msg.includes('detached Frame') || msg.includes('destroyed') || msg.includes('context') || msg.includes('Execution context')) {
        const delay = 1000 * (i + 1); // 1s, 2s, 3s, 4s — increasing backoff
        console.warn(`⚠️ Retrying (attempt ${i + 1}/${maxRetries}) in ${delay}ms due to: ${msg}`);
        await new Promise(resolve => setTimeout(resolve, delay));
        continue;
      }
      throw e;
    }
  }
  // Surface a clean error instead of raw Puppeteer message
  const cleanMsg = (lastError && lastError.message || '').includes('detached Frame')
    ? 'WhatsApp connection lost — please refresh your WhatsApp session in Settings.'
    : (lastError && lastError.message) || 'Unknown error';
  const err = new Error(cleanMsg);
  err.statusCode = 503;
  throw err;
}
// Alfred API — used to notify Python the moment a session becomes ready
// (e.g. to trigger immediate contact sync)
const ALFRED_API_URL = (process.env.ALFRED_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');

function normalizeDigits(value = '') {
  return String(value || '').replace(/\D/g, '');
}

function normalizeSessionId(sessionId = 'default') {
  return String(sessionId || 'default').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 80) || 'default';
}

function getSession(sessionId = 'default') {
  const id = normalizeSessionId(sessionId);
  if (!sessions.has(id)) {
    sessions.set(id, {
      id,
      client: null,
      isReady: false,
      initializing: false,
      initializePromise: null,
      qr: null,
      lastError: '',
      autoReplyEnabled: false,
      autoReplyMessage: "Hey! I'm currently busy and will get back to you soon. - Alfred",
      autoReplied: {},
    });
  }
  return sessions.get(id);
}

function getRequestSessionId(req, body = {}) {
  const url = new URL(req.url, 'http://localhost');
  const fromQuery = url.searchParams.get('sessionId');
  const fromHeader = req.headers['x-session-id'];
  const fromBody = body.sessionId;
  return normalizeSessionId(fromBody || fromHeader || fromQuery || 'default');
}

function buildSessionState(session) {
  return {
    sessionId: session.id,
    ready: session.isReady,
    qr: session.qr,
    lastError: session.lastError,
    initializing: session.initializing,
  };
}

async function createClient(sessionId = 'default') {
  const session = getSession(sessionId);
  if (session.client || session.initializing) return session;

  session.initializing = true;
  session.lastError = '';
  session.qr = null;

  const client = new Client({
    authStrategy: new LocalAuth({
      clientId: session.id,
      ...(WA_SESSION_PATH ? { dataPath: WA_SESSION_PATH } : {}),
    }),
    puppeteer: {
      headless: true,
      // Use the system Chromium installed in the Dockerfile
      executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-first-run',
        '--disable-extensions',
        '--disable-default-apps',
        '--mute-audio',
        '--hide-scrollbars',
        // NOTE: --single-process is intentionally REMOVED — it causes
        // 'detached Frame' crashes in Chromium and is not memory-safe.
      ]
    }
  });

  client.on('qr', async (qr) => {
    session.isReady = false;
    session.lastError = '';
    session.qr = await QRCode.toDataURL(qr);
    console.log(`📱 WhatsApp QR generated for session ${session.id}. Retrieve it from the Alfred web UI.`);
  });

  client.on('ready', () => {
    session.isReady = true;
    session.initializing = false;
    session.qr = null;
    session.lastError = '';
    console.log(`✅ Alfred is connected to WhatsApp for session ${session.id}!`);

    // Notify the Alfred Python API so it can sync contacts immediately.
    // Use http.request (not fetch) for reliability — Alfred may not be fully up
    // when the first ready event fires on startup.
    (() => {
      const body = JSON.stringify({ sessionId: session.id });
      const url = new URL(`${ALFRED_API_URL}/api/integrations/whatsapp/on-ready`);
      const options = {
        hostname: url.hostname,
        port: url.port || 8000,
        path: url.pathname,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      };
      const req = http.request(options, (res) => {
        console.log(`✅ Alfred on-ready webhook: ${res.statusCode}`);
      });
      req.on('error', (err) => {
        console.warn(`⚠️  Could not notify Alfred on-ready webhook: ${err.message}`);
      });
      req.write(body);
      req.end();
    })();
  });

  // Detect page-level disconnects (e.g. browser context destroyed) and auto-recover.
  client.on('change_state', (state) => {
    console.log(`🔄 WhatsApp state changed for session ${session.id}: ${state}`);
    if (state === 'CONFLICT' || state === 'UNLAUNCHED') {
      console.log(`♻️ Attempting to take over conflicted/unlaunched session ${session.id}...`);
      client.takeOver().catch((e) => console.warn(`takeOver error: ${e.message}`));
    }
  });

  client.on('disconnected', (reason) => {
    console.log(`⚠️ WhatsApp disconnected for session ${session.id}:`, reason);
    session.isReady = false;
    session.initializing = false;
    session.lastError = String(reason || 'Disconnected');
    session.client = null;
    console.log(`🔁 Will attempt to reconnect session ${session.id} in 5s...`);
    setTimeout(() => createClient(session.id), 5000);
  });

  client.on('auth_failure', (message) => {
    console.log(`❌ Authentication failed for session ${session.id}.`);
    session.isReady = false;
    session.initializing = false;
    session.lastError = message || 'Authentication failed';
    session.client = null;
  });

  session.client = client;

  session.initializePromise = client.initialize()
    .catch((e) => {
      session.lastError = e.message;
      session.client = null;
      session.isReady = false;
      session.qr = null;
    })
    .finally(() => {
      if (!session.isReady && !session.qr) {
        session.initializing = false;
      }
      session.initializePromise = null;
    });

  return session;
}

async function destroySession(sessionId = 'default') {
  const id = normalizeSessionId(sessionId);
  const session = sessions.get(id);
  if (session && session.client) {
    try {
      await session.client.destroy();
    } catch (_) {}
  }
  session && (session.initializePromise = null);
  sessions.delete(id);

  const authBase = WA_SESSION_PATH || path.join(process.cwd(), '.wwebjs_auth');
  const authDir = path.join(authBase, `session-${id}`);
  const cacheDir = path.join(process.cwd(), '.wwebjs_cache');
  fs.rmSync(authDir, { recursive: true, force: true });
  fs.rmSync(path.join(cacheDir, id), { recursive: true, force: true });
}

async function ensureReadySession(sessionId = 'default') {
  const session = await createClient(sessionId);
  if (!session.client || !session.isReady) {
    const error = new Error(session.lastError || 'WhatsApp not connected yet.');
    error.statusCode = 409;
    error.session = buildSessionState(session);
    throw error;
  }
  return session;
}

async function getIndividualMessages(session, limit = 10) {
  return withRetry(async () => {
    const chats = await session.client.getChats();
    const results = [];
    for (const chat of chats) {
      if (chat.isGroup) continue;
      const msgs = await chat.fetchMessages({ limit: 3 });
      if (!msgs.length) continue;
      const lastMsg = msgs[msgs.length - 1];
      const incoming = [...msgs].reverse().find(m => !m.fromMe);
      results.push({
        chatId: chat.id._serialized,
        from: chat.name || (incoming ? incoming.from : chat.id.user),
        message: lastMsg.body,
        time: new Date(lastMsg.timestamp * 1000).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }),
        unread: chat.unreadCount || 0
      });
      if (results.length >= limit) break;
    }
    return results;
  });
}

async function getRecentMessages(session, limit = 5) {
  return withRetry(async () => {
    const chats = await session.client.getChats();
    const results = [];
    for (const chat of chats.slice(0, 15)) {
      const messages = await chat.fetchMessages({ limit: 1 });
      if (!messages.length) continue;
      const msg = messages[0];
      if (!msg.fromMe) {
        results.push({
          chatId: chat.id._serialized,
          from: chat.name || msg.from,
          isGroup: chat.isGroup,
          message: msg.body,
          time: new Date(msg.timestamp * 1000).toLocaleTimeString()
        });
      }
      if (results.length >= limit) break;
    }
    return results;
  });
}

async function getContacts(session, limit = 300) {
  return withRetry(async () => {
    // Fetch contacts and chats in parallel for speed
    const [rawContacts, chats] = await Promise.all([
      session.client.getContacts(),
      session.client.getChats(),
    ]);

    // Build a map of chatId → lastMessageTimestamp so we can rank by activity.
    // getChats() already returns chats in most-recent-first order, so we also
    // record the chat's position as a fallback rank.
    const chatActivityMap = new Map();
    for (let i = 0; i < chats.length; i++) {
      const chat = chats[i];
      if (chat.isGroup) continue;
      const chatId = chat.id && chat.id._serialized ? chat.id._serialized : '';
      if (!chatId) continue;
      const lastMsgTs = chat.lastMessage ? chat.lastMessage.timestamp : 0;
      chatActivityMap.set(chatId, {
        lastMessageTimestamp: lastMsgTs || 0,
        // Use position as a tiebreaker (lower index = more recent)
        chatRank: i,
      });
    }

    const results = [];
    for (const contact of rawContacts) {
      if (!contact || contact.isGroup || contact.isMe || !contact.isWAContact) continue;

      // ── KEY FILTER: only sync contacts the user has explicitly saved ──
      // This excludes random numbers that appeared in chats but were never saved.
      if (!contact.isMyContact) continue;

      const chatId = contact.id && contact.id._serialized ? contact.id._serialized : '';

      // Skip @lid contacts — WhatsApp internal linked-device identifiers
      if (chatId.endsWith('@lid')) continue;

      // Only use contact.number — never fall back to contact.id.user
      const number = contact.number || '';
      const name = contact.name || contact.pushname || contact.shortName || number;
      if (!chatId && !number) continue;

      const activity = chatActivityMap.get(chatId) || { lastMessageTimestamp: 0, chatRank: 99999 };

      results.push({
        id: chatId,
        chatId,
        number,
        name,
        pushname: contact.pushname || '',
        isMyContact: true,
        isBusiness: Boolean(contact.isBusiness),
        lastMessageTimestamp: activity.lastMessageTimestamp,
        chatRank: activity.chatRank,
      });
    }

    // Sort: contacts with real chat activity come first, ordered by most recently
    // messaged. Contacts never messaged on WhatsApp go last, sorted alphabetically.
    results.sort((a, b) => {
      const aTs = a.lastMessageTimestamp;
      const bTs = b.lastMessageTimestamp;
      if (aTs !== bTs) return bTs - aTs; // most recent first
      if (a.chatRank !== b.chatRank) return a.chatRank - b.chatRank;
      return (a.name || '').localeCompare(b.name || '');
    });

    return results.slice(0, limit);
  });
}

async function resolveChatId(session, target) {
  const rawTarget = String(target || '').trim();
  if (!rawTarget) {
    const error = new Error('number is required');
    error.statusCode = 400;
    throw error;
  }
  if (rawTarget.includes('@')) {
    return rawTarget;
  }

  const digits = normalizeDigits(rawTarget);
  if (!digits) {
    const error = new Error('A valid WhatsApp number is required.');
    error.statusCode = 400;
    throw error;
  }

  try {
    const numberId = await session.client.getNumberId(digits);
    if (numberId && numberId._serialized) {
      return numberId._serialized;
    }
  } catch (_) {}

  try {
    const contacts = await session.client.getContacts();
    const directMatch = contacts.find((contact) => {
      const contactId = contact?.id?._serialized || '';
      const contactNumber = normalizeDigits(contact?.number || contact?.id?.user || '');
      return (
        (contactNumber && (contactNumber === digits || contactNumber.endsWith(digits) || digits.endsWith(contactNumber))) ||
        (contactId && normalizeDigits(contactId.split('@', 1)[0]).endsWith(digits))
      );
    });
    if (directMatch?.id?._serialized) {
      return directMatch.id._serialized;
    }
  } catch (_) {}

  return `${digits}@c.us`;
}

async function checkAndAutoReply() {
  for (const session of sessions.values()) {
    if (!session.autoReplyEnabled || !session.isReady || !session.client) continue;
    try {
      const chats = await session.client.getChats();
      for (const chat of chats.slice(0, 20)) {
        if (chat.isGroup) continue;
        const msgs = await chat.fetchMessages({ limit: 3 });
        if (!msgs.length) continue;
        const last = msgs[msgs.length - 1];
        if (!last.fromMe) {
          const id = chat.id._serialized;
          const now = Date.now();
          if (session.autoReplied[id] && now - session.autoReplied[id] < 3600000) continue;
          await chat.sendMessage(session.autoReplyMessage);
          session.autoReplied[id] = now;
          console.log(`Auto-replied to ${chat.name || id} for session ${session.id}`);
        }
      }
    } catch (e) {
      console.log(`Auto-reply check error for ${session.id}:`, e.message);
    }
  }
}
setInterval(checkAndAutoReply, 60000);

const server = http.createServer(async (req, res) => {
  const sendJson = (statusCode, payload) => {
    res.writeHead(statusCode, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(payload));
  };

  const sendBridgeError = (error, session = null) => {
    const statusCode = Number.isInteger(error && error.statusCode) ? error.statusCode : 500;
    sendJson(statusCode, {
      ok: false,
      error: error && error.message ? error.message : 'Unknown error',
      ...(session ? buildSessionState(session) : (error && error.session ? error.session : {})),
    });
  };

  if (req.method === 'POST' && req.url === '/session/start') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
      try {
        const parsed = body ? JSON.parse(body) : {};
        const sessionId = getRequestSessionId(req, parsed);
        const session = await createClient(sessionId);
        sendJson(200, { ok: true, ...buildSessionState(session) });
      } catch (e) {
        sendBridgeError(e);
      }
    });
  } else if (req.method === 'GET' && req.url.startsWith('/session/status')) {
    const sessionId = getRequestSessionId(req);
    const session = getSession(sessionId);
    sendJson(200, { ok: true, ...buildSessionState(session) });
  } else if (req.method === 'DELETE' && req.url.startsWith('/session/logout')) {
    const sessionId = getRequestSessionId(req);
    try {
      await destroySession(sessionId);
      sendJson(200, { ok: true });
    } catch (e) {
      sendJson(500, { ok: false, error: e.message });
    }
  } else if (req.method === 'POST' && req.url === '/send') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
      try {
        const parsed = JSON.parse(body);
        const sessionId = getRequestSessionId(req, parsed);
        const session = await ensureReadySession(sessionId);
        const { number, message } = parsed;
        const chatId = await withRetry(async () => await resolveChatId(session, number));
        await withRetry(async () => await session.client.sendMessage(chatId, message));
        sendJson(200, { ok: true, chatId });
      } catch (e) {
        sendBridgeError(e);
      }
    });
  } else if (req.method === 'POST' && req.url === '/reply') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
      try {
        const parsed = JSON.parse(body);
        const sessionId = getRequestSessionId(req, parsed);
        const session = await ensureReadySession(sessionId);
        const { chatId, message } = parsed;
        if (!chatId || !message) {
          sendJson(400, { ok: false, error: 'chatId and message are required' });
          return;
        }
        await withRetry(async () => await session.client.sendMessage(chatId, message));
        sendJson(200, { ok: true });
      } catch (e) {
        sendBridgeError(e);
      }
    });
  } else if (req.method === 'POST' && req.url === '/autoreply') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      try {
        const parsed = body ? JSON.parse(body) : {};
        const sessionId = getRequestSessionId(req, parsed);
        const session = getSession(sessionId);
        session.autoReplyEnabled = Boolean(parsed.enabled);
        if (parsed.message) session.autoReplyMessage = parsed.message;
        session.autoReplied = {};
        sendJson(200, { ok: true, enabled: session.autoReplyEnabled, message: session.autoReplyMessage });
      } catch (e) {
        sendJson(500, { ok: false, error: e.message });
      }
    });
  } else if (req.method === 'GET' && req.url.startsWith('/autoreply')) {
    const sessionId = getRequestSessionId(req);
    const session = getSession(sessionId);
    sendJson(200, { enabled: session.autoReplyEnabled, message: session.autoReplyMessage });
  } else if (req.method === 'GET' && req.url.startsWith('/status')) {
    const sessionId = getRequestSessionId(req);
    const session = getSession(sessionId);
    sendJson(200, { ready: session.isReady, sessionId });
  } else if (req.method === 'GET' && req.url.startsWith('/messages/individual')) {
    try {
      const url = new URL(req.url, 'http://localhost');
      const limit = parseInt(url.searchParams.get('limit') || '10', 10);
      const sessionId = getRequestSessionId(req);
      const session = await ensureReadySession(sessionId);
      const messages = await getIndividualMessages(session, limit);
      sendJson(200, messages);
    } catch (e) {
      sendBridgeError(e);
    }
  } else if (req.method === 'GET' && req.url.startsWith('/contacts')) {
    try {
      const url = new URL(req.url, 'http://localhost');
      const limit = parseInt(url.searchParams.get('limit') || '300', 10);
      const sessionId = getRequestSessionId(req);
      const session = await ensureReadySession(sessionId);
      const contacts = await getContacts(session, limit);
      sendJson(200, contacts);
    } catch (e) {
      sendBridgeError(e);
    }
  } else if (req.method === 'GET' && req.url.startsWith('/messages')) {
    try {
      const url = new URL(req.url, 'http://localhost');
      const limit = parseInt(url.searchParams.get('limit') || '5', 10);
      const sessionId = getRequestSessionId(req);
      const session = await ensureReadySession(sessionId);
      const messages = await getRecentMessages(session, limit);
      sendJson(200, messages);
    } catch (e) {
      sendBridgeError(e);
    }
  } else {
    res.writeHead(404);
    res.end();
  }
});

server.on('error', (error) => {
  console.error(`WhatsApp bridge failed to listen on ${BRIDGE_HOST}:${BRIDGE_PORT}:`, error);
  process.exit(1);
});

server.listen(BRIDGE_PORT, BRIDGE_HOST, () => {
  console.log(`🌐 WhatsApp bridge running on http://${BRIDGE_HOST}:${BRIDGE_PORT}`);
});
