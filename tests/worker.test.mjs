import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto, createHash} from 'node:crypto';
import fs from 'node:fs';
import YAML from 'yaml';
import worker from '../worker.mjs';
// Public limits verified by behavior; the Worker entry exports only its handler.
const SOURCE_TIMEOUT_MS = 5000, MAX_SOURCE_BYTES = 65536, MAX_ENDPOINTS = 128;

globalThis.crypto ??= webcrypto;
globalThis.fetch = () => {throw new Error('Outbound network forbidden');};
const token = 'A'.repeat(43);
const uuid = '00000000-0000-4000-8000-000000000000';
const config = {
  subscription_domain: 'sub.example.com', domain: 'node.example.com', uuid,
  token_sha256: createHash('sha256').update(token).digest('hex'),
  routes: ['vless', 'trojan', 'vmess'].map(protocol => ({protocol, path: `/路径/${protocol}?ed=2048`})),
  preferred: [{address: '2001:db8::1', name: 'IPv6 测试 "quoted"'}],
};
const env = {SUB_CONFIG: JSON.stringify(config)};
function get(path = `/s/${token}`, options, environment = env) {
  return worker.fetch(new Request(`https://sub.example.com${path}`, options), environment);
}

test('unauthorized requests disclose no configuration', async () => {
  for (const path of ['/', '/sub', '/s/' + 'B'.repeat(43), '/s/' + uuid]) {
    const response = await get(path);
    assert.equal(response.status, 404);
    assert(!((await response.text()).includes(uuid)));
    assert.match(response.headers.get('Cache-Control'), /no-store/);
  }
});

test('custom hostname enforced even with valid token', async () => {
  assert.equal((await worker.fetch(new Request(`https://other.workers.dev/s/${token}`), env)).status, 404);
});

test('raw subscription preserves credentials, TLS, Unicode paths, and IPv6', async () => {
  const response = await get(`/s/${token}?format=raw`);
  assert.equal(response.status, 200);
  const lines = (await response.text()).split('\n');
  assert.equal(lines.length, 6);
  const vless = new URL(lines[0]);
  assert.equal(vless.hostname, config.domain);
  assert.equal(vless.searchParams.get('host'), config.domain);
  assert.equal(vless.searchParams.get('sni'), config.domain);
  assert.equal(vless.searchParams.get('path'), config.routes[0].path);
  assert.equal(vless.searchParams.get('security'), 'tls');
  assert(lines[1].includes('@[2001:db8::1]:443'));
  assert(lines[2].startsWith(`trojan://${uuid}@`));
  const vmess = JSON.parse(Buffer.from(lines[4].slice(8), 'base64').toString());
  assert.equal(vmess.id, uuid);
  assert.equal(vmess.host, config.domain);
  assert.equal(vmess.path, config.routes[2].path);
});

test('base64 and protocol selection round trip correctly', async () => {
  const raw = await (await get(`/s/${token}?format=raw&protocol=trojan`)).text();
  const encoded = await (await get(`/s/${token}?format=base64&protocol=trojan`)).text();
  assert.equal(Buffer.from(encoded, 'base64').toString(), raw);
  assert(raw.split('\n').every(line => line.startsWith('trojan://')));
});

test('Clash/Mihomo output uses correct protocol-specific credential fields', async () => {
  const response = await get(`/s/${token}?format=clash`);
  assert.match(response.headers.get('Content-Type'), /yaml/);
  const body = YAML.parse(await response.text());
  assert.equal(body.proxies.length, 6);
  for (const proxy of body.proxies) {
    assert.equal(proxy['skip-cert-verify'], false);
    assert.equal(proxy['ws-opts'].headers.Host, config.domain);
    if (proxy.type === 'trojan') {
      assert.equal(proxy.password, uuid);
      assert(!('uuid' in proxy));
    } else assert.equal(proxy.uuid, uuid);
  }
  assert.equal(body.proxies[4].cipher, 'auto');
  assert.equal(body['proxy-groups'][0].proxies.length, 7);
  assert.equal(body['proxy-groups'][0].proxies[0], '自动选择');
  assert.equal(body['proxy-groups'][1].type, 'url-test');
  assert.equal(body['proxy-groups'][1].proxies.length, 6);
});

test('default subscription is directly importable Clash YAML, not Base64', async () => {
  const response = await get(`/s/${token}`);
  const text = await response.text();
  assert(text.startsWith('mixed-port: 7890\n'));
  const body = YAML.parse(text);
  assert.equal(body.proxies.length, 6);
  assert.equal(body.proxies[0]['ws-opts'].path, config.routes[0].path);
  assert.equal(body.proxies[1].server, '2001:db8::1');
  assert.equal(body.proxies[1].name, 'VLESS · IPv6 测试 "quoted" · 2');
});

test('old token rejected after rotation, new token and Bearer accepted', async () => {
  const newToken = 'C'.repeat(43);
  const changed = {SUB_CONFIG: JSON.stringify({...config, token_sha256: createHash('sha256').update(newToken).digest('hex')})};
  assert.equal((await get(`/s/${token}`, undefined, changed)).status, 404);
  assert.equal((await get(`/s/${newToken}`, undefined, changed)).status, 200);
  assert.equal((await get('/sub', {headers: {Authorization: `Bearer ${newToken}`}}, changed)).status, 200);
});

test('friendly filename route keeps all protocols and old URLs remain compatible', async () => {
  const legacy = await get(`/s/${token}`);
  const friendly = await get(`/s/${token}/Private-XUI.yaml`);
  assert.equal(friendly.status, 200);
  assert.equal(await legacy.text(), await friendly.text());
  const filtered = await get(`/s/${token}/Private-XUI.yaml?protocol=trojan`);
  assert(YAML.parse(await filtered.text()).proxies.every(proxy => proxy.type === 'trojan'));
  assert.equal((await get(`/s/${'B'.repeat(43)}/Private-XUI.yaml`)).status, 404);
  assert.equal((await get(`/s/${token}/arbitrary.yaml`)).status, 404);
});

test('subscription name uses safe Unicode headers rather than a token filename', async () => {
  const named = {SUB_CONFIG: JSON.stringify({...config, subscription_name: '我的 VPN 订阅'})};
  const response = await get(undefined, undefined, named);
  const disposition = response.headers.get('Content-Disposition');
  assert.match(disposition, /filename="Private-XUI.yaml"/);
  assert(disposition.includes(`filename*=UTF-8''${encodeURIComponent('我的 VPN 订阅.yaml')}`));
  assert.equal(Buffer.from(response.headers.get('Profile-Title').slice(7), 'base64').toString(), '我的 VPN 订阅');
  assert.equal(response.headers.get('Profile-Update-Interval'), '24');
  for (const [key, value] of response.headers) {
    assert(!value.includes(token), `${key} leaked subscription token`);
    assert(!value.includes(uuid), `${key} leaked node credential`);
  }
  const head = await get(undefined, {method: 'HEAD'}, named);
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('Content-Disposition'), disposition);
  const malicious = {SUB_CONFIG: JSON.stringify({...config, subscription_name: 'Bad\r\nHeader:"/\\\tname'})};
  assert.equal((await get(undefined, undefined, malicious)).status, 200);
  const unauthorized = await get('/s/' + 'B'.repeat(43), undefined, named);
  assert.equal(unauthorized.headers.get('Content-Disposition'), null);
  assert.equal(unauthorized.headers.get('Profile-Title'), null);
});

test('no preferred addresses means one entry per configured protocol, not one overall', async () => {
  const direct = {SUB_CONFIG: JSON.stringify({...config, preferred: []})};
  const all = YAML.parse(await (await get(undefined, undefined, direct)).text());
  assert.deepEqual(all.proxies.map(proxy => proxy.type), ['vless', 'trojan', 'vmess']);
  assert(all.proxies.every(proxy => proxy.name.includes('域名入口')));
  const vless = YAML.parse(await (await get(`/s/${token}?protocol=vless`, undefined, direct)).text());
  assert.equal(vless.proxies.length, 1);
  assert.equal(vless['proxy-groups'].length, 1);
});

test('six candidate entries produce 21 usable definitions and client-side latency selection', async () => {
  const preferred = Array.from({length: 6}, (_, i) => ({address: `104.${16 + i}.0.1`, name: `CF 候选 ${i + 1}`}));
  const candidates = {SUB_CONFIG: JSON.stringify({...config, preferred})};
  const response = await get(undefined, undefined, candidates);
  const body = YAML.parse(await response.text());
  assert.equal(body.proxies.length, 21);
  const automatic = body['proxy-groups'].find(group => group.type === 'url-test');
  assert.deepEqual(automatic.proxies, body.proxies.map(proxy => proxy.name));
  assert.equal(automatic.url, 'https://www.gstatic.com/generate_204');
  for (const protocol of ['vless', 'trojan', 'vmess']) {
    const entries = body.proxies.filter(proxy => proxy.type === protocol);
    assert.equal(entries.length, 7);
    assert.equal(entries[0].server, config.domain);
    for (const proxy of entries) {
      assert.equal(proxy['ws-opts'].headers.Host, config.domain);
      assert.equal(proxy.sni || proxy.servername, config.domain);
      assert.equal(proxy.password || proxy.uuid, config.uuid);
      assert.equal(proxy['skip-cert-verify'], false);
    }
  }
});

test('duplicate entry points do not create duplicate nodes', async () => {
  const duplicated = {SUB_CONFIG: JSON.stringify({...config, preferred: [
    {address: config.domain.toUpperCase(), name: 'Duplicate domain'},
    ...config.preferred, ...config.preferred,
  ]})};
  const body = YAML.parse(await (await get(undefined, undefined, duplicated)).text());
  assert.equal(body.proxies.length, 6);
  assert.equal(new Set(body.proxies.map(proxy => proxy.name)).size, 6);
});

test('override parameters and unsupported methods/formats are rejected', async () => {
  for (const suffix of ['?domain=evil.example', '?path=/other', '?uuid=fake', '?format=surge', '?protocol=bad']) {
    assert.equal((await get(`/s/${token}${suffix}`)).status, 400);
  }
  assert.equal((await get(`/s/${token}`, {method: 'POST'})).status, 405);
  assert.equal(await (await get(`/s/${token}`, {method: 'HEAD'})).text(), '');
});

test('malformed config returns generic error and source has no telemetry or converters', async () => {
  const response = await get(undefined, undefined, {SUB_CONFIG: 'not JSON'});
  assert.equal(response.status, 503);
  assert.equal(await response.text(), 'Subscription unavailable');
  const source = fs.readFileSync(new URL('../worker.mjs', import.meta.url), 'utf8');
  assert(!/console\.|yx-auto|url\.v1\.mk/.test(source));
});

function autoEnv(extra = {}) {
  return {SUB_CONFIG: JSON.stringify({...config, preferred_mode: 'auto', preferred: [], ...extra})};
}

function publicData(count = 12, url = 'https://cf.090227.xyz/ct?ips=12') {
  const group = new URL(url).pathname === '/cu' ? 1 : new URL(url).pathname === '/cmcc' ? 2 : 0;
  return Array.from({length: count}, (_, i) => `104.16.${group}.${i + 1}#1ms 99MB/s`).join('\n');
}

test('auto mode merges domain pool plus three carrier IP sources without disclosing private config', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({url, options});
    return new Response(publicData(12, url));
  });
  const response = await get(`/s/${token}/Private-XUI.yaml?protocol=vless`, {
    headers: {Authorization: `Bearer ${token}`, Cookie: `secret=${token}`, Referer: `https://${config.domain}/${uuid}`},
  }, autoEnv());
  assert.equal(response.status, 200);
  const body = YAML.parse(await response.text());
  assert.equal(body.proxies.length, 48); // 1 domain + 11 pooled domains + 36 distinct IPs
  assert(body.proxies.every(proxy => proxy.type === 'vless'));
  assert.equal(body.proxies[0].server, config.domain);
  assert(body.proxies.some(proxy => proxy.name.includes('电信')));
  assert(!body.proxies.some(proxy => /1ms|99MB/.test(proxy.name))); // source measurements aren't user's
  assert.equal(calls.length, 3);
  assert.deepEqual(calls.map(c => c.url).sort(), ['https://cf.090227.xyz/cmcc?ips=12', 'https://cf.090227.xyz/ct?ips=12', 'https://cf.090227.xyz/cu?ips=12']);
  const call = calls[0];
  assert.equal(call.options.method, 'GET');
  assert.equal(call.options.redirect, 'manual');
  assert.equal(call.options.credentials, 'omit');
  assert.equal(call.options.body, undefined);
  const sent = JSON.stringify(calls);
  for (const secret of [uuid, token, config.domain, config.subscription_domain, ...config.routes.map(route => route.path)]) {
    assert(!sent.includes(secret), `private value sent to public list provider`);
  }
});

test('auto mode supports every configured protocol and keeps TLS verification enabled', async t => {
  t.mock.method(globalThis, 'fetch', async url => new Response(publicData(12, url)));
  const body = YAML.parse(await (await get(undefined, undefined, autoEnv())).text());
  assert.equal(body.proxies.length, 144);
  for (const protocol of ['vless', 'trojan', 'vmess']) {
    const entries = body.proxies.filter(proxy => proxy.type === protocol);
    assert.equal(entries.length, 48);
    assert(entries.every(proxy => (proxy.sni || proxy.servername) === config.domain));
    assert(entries.every(proxy => proxy['skip-cert-verify'] === false));
  }
});

test('bad public addresses are rejected and repeated IPv4/IPv6 entries are deduplicated', async t => {
  const bad = ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.0.2.1', '8.8.8.8', '::1',
    'fd00::1', 'fe80::1', '2606:4700::1/128', 'https://evil.example', 'node.example.com', '104.16.0.999'];
  const addresses = [...bad, '104.18.1.1', '104.18.1.1', '2606:4700::1', '2606:4700:0:0:0:0:0:1'];
  t.mock.method(globalThis, 'fetch', async () => new Response(addresses.join('\n')));
  const body = YAML.parse(await (await get(`/s/${token}?protocol=vless`, undefined, autoEnv())).text());
  assert.equal(body.proxies.length, 14);
  assert.equal(body.proxies.filter(proxy => proxy.server === '104.18.1.1').length, 1);
  assert.equal(body.proxies.filter(proxy => proxy.server === '2606:4700::1').length, 1);
  assert(body.proxies.slice(1).every(proxy => !bad.includes(proxy.server)));
});

test('optional GitHub source is fixed, excludes non-CF hosts and keeps only port 443', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({url, options});
    if (url.startsWith('https://cf.090227.xyz/')) return new Response(publicData(1, url));
    return new Response('104.18.9.1:8443#unsupported\n104.18.9.3:443#supported\n[2606:4700::88]:443#IPv6\n104.18.9.2:80#not TLS\n8.8.8.8:443\nhttps://evil.example\nsaas.example#arbitrary host');
  });
  const body = YAML.parse(await (await get(`/s/${token}?protocol=vless`, undefined, autoEnv({preferred_github: true}))).text());
  assert.equal(calls.length, 4);
  assert(calls.some(call => call.url === 'https://raw.githubusercontent.com/qwer-search/bestip/refs/heads/main/kejilandbestip.txt'));
  assert.equal(body.proxies.find(proxy => proxy.server === '104.18.9.3').port, 443);
  assert(body.proxies.some(proxy => proxy.server === '2606:4700::88'));
  assert(!body.proxies.some(proxy => ['104.18.9.1', '104.18.9.2', '8.8.8.8', 'saas.example'].includes(proxy.server)));
  for (const call of calls) {
    const encoded = JSON.stringify(call);
    for (const secret of [uuid, token, config.domain, config.subscription_domain, ...config.routes.map(route => route.path)]) {
      assert(!encoded.includes(secret));
    }
  }
});

test('manually configured entry ports normalize to 443 and deduplicate consistently', async () => {
  const preferred = [
    {address: '104.18.9.1', port: 8443, name: 'manual port'},
    {address: '104.18.9.1', port: 443, name: 'same entry'},
    {address: config.domain, port: 2053, name: 'same domain'},
  ];
  const environment = {SUB_CONFIG: JSON.stringify({...config, preferred})};
  const body = YAML.parse(await (await get(undefined, undefined, environment)).text());
  assert.equal(body.proxies.length, 6);
  assert(body.proxies.every(proxy => proxy.port === 443));
  for (const type of ['vless', 'trojan', 'vmess']) {
    assert.equal(body.proxies.filter(proxy => proxy.type === type && proxy.server === '104.18.9.1').length, 1);
  }
});

test('source failures preserve DNS and domain pool with six explicitly named fallback candidates', async t => {
  for (const result of [new Response('unavailable', {status: 503}), new Response('malformed'), Response.json({data: {}})]) {
    t.mock.method(globalThis, 'fetch', async () => result.clone());
    const response = await get(`/s/${token}?protocol=vless`, undefined, autoEnv());
    assert.equal(response.status, 200);
    const body = YAML.parse(await response.text());
    assert.equal(body.proxies.length, 18);
    assert.equal(body.proxies.filter(proxy => proxy.name.includes('CF 候选')).length, 6);
    assert.equal(body.proxies[0].server, config.domain);
  }
});

test('source deadline includes a stalled response body and aborts all source requests', async t => {
  const signals = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    signals.push(options.signal);
    return new Response(new ReadableStream({start() {}}));
  });
  const start = Date.now();
  const response = await get(`/s/${token}?protocol=vless`, undefined, autoEnv({preferred_github: true}));
  const elapsed = Date.now() - start;
  assert(elapsed < SOURCE_TIMEOUT_MS + 2000, `source timeout took ${elapsed} ms`);
  assert.equal(response.status, 200);
  assert.equal(YAML.parse(await response.text()).proxies.length, 18);
  assert.equal(signals.length, 4);
  assert(signals.every(signal => signal.aborted));
});

test('source response byte limit and endpoint limit bound generated subscriptions', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(' '.repeat(MAX_SOURCE_BYTES + 1)));
  let body = YAML.parse(await (await get(`/s/${token}?protocol=vless`, undefined, autoEnv())).text());
  assert.equal(body.proxies.length, 18);
  t.mock.method(globalThis, 'fetch', async url => new Response(publicData(200, url)));
  body = YAML.parse(await (await get(`/s/${token}?protocol=vless`, undefined, autoEnv())).text());
  assert.equal(body.proxies.length, MAX_ENDPOINTS);
});

test('unauthorized, unknown query URLs, and offline modes cannot trigger public source fetches', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async url => {calls.push(url); return new Response(publicData(12, url));});
  for (const query of ['source=https://127.0.0.1', 'piu=https://evil.example', 'preferred_url=https://evil.example']) {
    assert.equal((await get(`/s/${token}?${query}`, undefined, autoEnv())).status, 400);
  }
  assert.equal((await get('/s/' + 'B'.repeat(43), undefined, autoEnv())).status, 404);
  for (const mode of [undefined, 'direct', 'custom', 'builtin']) {
    assert.equal((await get(undefined, undefined, autoEnv({preferred_mode: mode}))).status, 200);
  }
  assert.equal(calls.length, 0);
});

test('one unavailable carrier does not remove other carrier lists', async t => {
  t.mock.method(globalThis, 'fetch', async url => url.includes('/cu?')
    ? new Response('offline', {status: 503}) : new Response(publicData(12, url)));
  const body = YAML.parse(await (await get(`/s/${token}?protocol=vless`, undefined, autoEnv())).text());
  assert.equal(body.proxies.length, 36); // 12 domain entries + 24 IPs
  assert(body.proxies.some(p => p.name.includes('电信')));
  assert(body.proxies.some(p => p.name.includes('移动')));
  assert(!body.proxies.some(p => p.name.includes('CF 候选')));
});
