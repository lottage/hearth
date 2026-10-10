'use strict';
// Computer: Alexa custom skill (Alexa-hosted, Node.js). It owns no logic: every request is sealed and posted to a relay topic, the
// house (StoneSage, outbound-only) answers on a second topic, and this speaks the answer. Protocol: StoneSage/backend/alexa_relay.py.
// index.template.js is committed with placeholders; build_skill.py fills in the key and phrase table (the output is NOT committed).

const crypto = require('crypto');
const https = require('https');

const KEY = '__RELAY_KEY__';
const RELAY_HOST = 'ntfy.sh';
const WAIT_MS = 6500;          // Alexa gives a skill ~8 s; the house is told this as the deadline
const CARRIERS = __CARRIERS__; // intent name -> the words that started the sentence (Alexa only gives us the slot after them)
const SIMPLE = __SIMPLE__;     // intent name -> the whole sentence (hello, thanks...)

// ---- sealing: must match alexa_relay.py exactly -------------------------------------------------------------------------
const mac = (key, data) => crypto.createHmac('sha256', key).update(data).digest();
function derive(key) {
  const k = Buffer.from(key, 'utf8');
  return { enc: mac(k, 'ss-relay enc'), mac: mac(k, 'ss-relay mac'),
           req: 'ssr-' + mac(k, 'ss-relay topic req').toString('hex').slice(0, 32),
           res: 'ssr-' + mac(k, 'ss-relay topic res').toString('hex').slice(0, 32) };
}
function keystream(enc, nonce, n) {
  const parts = [];
  for (let i = 0, got = 0; got < n; i++) {
    const ctr = Buffer.alloc(4); ctr.writeUInt32BE(i);
    const block = mac(enc, Buffer.concat([nonce, ctr])); parts.push(block); got += block.length;
  }
  return Buffer.concat(parts).subarray(0, n);
}
const b64url = (b) => b.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
function seal(keys, label, obj) {
  const pt = Buffer.from(JSON.stringify(obj), 'utf8');
  const nonce = crypto.randomBytes(16);
  const ks = keystream(keys.enc, nonce, pt.length);
  const ct = Buffer.from(pt.map((b, i) => b ^ ks[i]));
  const tag = mac(keys.mac, Buffer.concat([Buffer.from(label), Buffer.from([0]), nonce, ct]));
  return b64url(Buffer.concat([nonce, ct, tag]));
}
function open(keys, label, text) {
  const raw = Buffer.from(String(text).trim().replace(/-/g, '+').replace(/_/g, '/'), 'base64');
  if (raw.length < 50) throw new Error('too short');
  const nonce = raw.subarray(0, 16), ct = raw.subarray(16, raw.length - 32), tag = raw.subarray(raw.length - 32);
  const want = mac(keys.mac, Buffer.concat([Buffer.from(label), Buffer.from([0]), nonce, ct]));
  if (!crypto.timingSafeEqual(tag, want)) throw new Error('bad tag');
  const ks = keystream(keys.enc, nonce, ct.length);
  return JSON.parse(Buffer.from(ct.map((b, i) => b ^ ks[i])).toString('utf8'));
}

// ---- the relay (replaceable in tests) ----------------------------------------------------------------------------------
function publish(topic, body) {
  return new Promise((resolve, reject) => {
    const req = https.request({ host: RELAY_HOST, path: '/' + topic, method: 'POST',
      headers: { 'Content-Type': 'text/plain', 'Content-Length': Buffer.byteLength(body) } }, (res) => {
      res.resume(); res.on('end', () => (res.statusCode < 300 ? resolve() : reject(new Error('relay ' + res.statusCode))));
    });
    req.on('error', reject); req.setTimeout(5000, () => req.destroy(new Error('relay timeout'))); req.end(body);
  });
}
function waitFor(keys, id, sinceSec, ms) {
  // streams the answer topic until the answer to `id` arrives (or `ms` pass); resolves null on timeout
  return new Promise((resolve) => {
    let done = false, req = null;
    const finish = (v) => { if (!done) { done = true; clearTimeout(timer); if (req) req.destroy(); resolve(v); } };
    const timer = setTimeout(() => finish(null), ms);
    req = https.get({ host: RELAY_HOST, path: '/' + keys.res + '/json?since=' + sinceSec }, (res) => {
      let buf = '';
      res.on('data', (d) => {
        buf += d.toString('utf8');
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          try {
            const ev = JSON.parse(line);
            if (ev.event !== 'message') continue;
            const ans = open(keys, 'res', ev.message);
            if (ans.re === id) return finish(ans);
          } catch (e) { /* not ours or not for this request: ignore */ }
        }
      });
    });
    req.on('error', () => finish(null));
  });
}
const relay = { publish, waitFor };

// ---- the skill -------------------------------------------------------------------------------------------------------
const say = (text, end, reprompt) => ({ version: '1.0', response: Object.assign(
  { outputSpeech: { type: 'PlainText', text }, shouldEndSession: end },
  reprompt ? { reprompt: { outputSpeech: { type: 'PlainText', text: reprompt } } } : {}) });

function utterance(intent) {
  const name = intent.name;
  if (name === 'AMAZON.YesIntent') return 'yes';
  if (name === 'AMAZON.NoIntent') return 'no';
  if (SIMPLE[name]) return SIMPLE[name];
  if (CARRIERS[name]) {
    const slot = intent.slots && intent.slots.query && intent.slots.query.value;
    return (CARRIERS[name] + ' ' + (slot || '')).trim();
  }
  return null;
}

async function ask(event, text) {
  const keys = derive(KEY);
  const sys = (event.context && event.context.System) || {};
  const id = crypto.randomBytes(8).toString('hex');
  const hash = (s) => crypto.createHash('sha256').update(String(s || '')).digest('hex').slice(0, 16);
  const nowSec = Math.floor(Date.now() / 1000);
  const waiter = relay.waitFor(keys, id, nowSec - 5, WAIT_MS);   // listening first, so a fast answer cannot be missed
  try {
    await relay.publish(keys.req, seal(keys, 'req', { v: 1, id, ts: nowSec, sid: (event.session || {}).sessionId || id,
      text, dl: WAIT_MS / 1000, uid: hash((sys.user || {}).userId), dev: hash((sys.device || {}).deviceId) }));
  } catch (e) {
    return say("I can't reach the house right now.", true);
  }
  const ans = await waiter;
  if (!ans) return say("That's taking a while. I'll announce it when it's ready.", true);
  return ans.more ? say(ans.say, false, 'Yes or no?') : say(ans.say, true);
}

async function handler(event) {
  const req = event.request || {};
  if (req.type === 'LaunchRequest') return say('Computer here. What do you need?', false, 'Go ahead.');
  if (req.type === 'SessionEndedRequest') return { version: '1.0', response: {} };
  if (req.type !== 'IntentRequest') return say('Sorry, I do not understand that.', true);
  const name = req.intent.name;
  if (['AMAZON.StopIntent', 'AMAZON.CancelIntent', 'AMAZON.NavigateHomeIntent'].includes(name)) return say('Right.', true);
  if (name === 'AMAZON.HelpIntent') {
    return say('Ask me to turn something on or off, or ask where someone is, or what the temperature is.', false, 'What do you need?');
  }
  const text = utterance(req.intent);
  if (!text) return say('I did not catch that. Start with turn, is, where, or what.', false, 'Try again.');
  return ask(event, text);
}

module.exports = { handler, _test: { derive, seal, open, utterance, relay, WAIT_MS } };
