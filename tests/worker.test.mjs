import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto, createHash} from 'node:crypto';
import fs from 'node:fs';
import YAML from 'yaml';
import worker from '../worker.mjs';

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
  assert.equal(body['proxy-groups'][0].proxies.length, 6);
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

test('override parameters and unsupported methods/formats are rejected', async () => {
  for (const suffix of ['?domain=evil.example', '?path=/other', '?uuid=fake', '?format=surge', '?protocol=bad']) {
    assert.equal((await get(`/s/${token}${suffix}`)).status, 400);
  }
  assert.equal((await get(`/s/${token}`, {method: 'POST'})).status, 405);
  assert.equal(await (await get(`/s/${token}`, {method: 'HEAD'})).text(), '');
});

test('malformed config returns generic error and source has no outgoing calls', async () => {
  const response = await get(undefined, undefined, {SUB_CONFIG: 'not JSON'});
  assert.equal(response.status, 503);
  assert.equal(await response.text(), 'Subscription unavailable');
  const source = fs.readFileSync(new URL('../worker.mjs', import.meta.url), 'utf8');
  assert(!/\bfetch\s*\(/.test(source.replace('async fetch(request, env)', 'async handler(request, env)')));
  assert(!/console\.|yx-auto|url\.v1\.mk/.test(source));
});
