import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
import YAML from 'yaml';
test('actual workerd fetches all carrier APIs and does not follow source redirects',async t=>{
  const token='A'.repeat(43),calls=[];let redirect=false;
  const config={subscription_domain:'sub.example.test',domain:'node.example.test',uuid:'01234567-89ab-4cde-8123-456789abcdef',token_sha256:createHash('sha256').update(token).digest('hex'),routes:[{protocol:'vless',path:'/fixture'}],preferred_mode:'auto',preferred:[]};
  const mf=new Miniflare(convertV4MiniflareOptions({modules:[{type:'ESModule',path:'worker.mjs'}],compatibilityDate:'2026-10-01',bindings:{SUB_CONFIG:JSON.stringify(config)},outboundService:request=>{
    calls.push(request.url);assert.equal(request.headers.get('authorization'),null);assert.equal(request.headers.get('cookie'),null);
    if(redirect)return new Response(null,{status:302,headers:{Location:'https://untrusted.example/'}});
    const id=request.url.includes('/ct?')?1:request.url.includes('/cu?')?2:3;
    return new Response(`104.17.1.${id}#public source`);
  }}));t.after(()=>mf.dispose());
  const url=`https://sub.example.test/s/${token}`;
  const result=await mf.dispatchFetch(url);assert.equal(result.status,200);
  const body=YAML.parse(await result.text());assert.equal(body.proxies.length,15);assert.equal(body.proxies.filter(p=>p.name.includes('公开优选')).length,3);
  redirect=true;const fallback=YAML.parse(await(await mf.dispatchFetch(url)).text());assert.equal(fallback.proxies.length,18);
  assert.equal(calls.length,6);assert(calls.every(u=>u.startsWith('https://cf.090227.xyz/')));
});

test('actual workerd separates SOCKS and direct profiles without sharing either credential with sources', async t => {
  const token = 'B'.repeat(43);
  const uuid = '01234567-89ab-4cde-8123-456789abcdef';
  const egressUuid = '11111111-1111-4111-8111-111111111111';
  const config = {
    subscription_domain: 'sub.example.test', domain: 'node.example.test', uuid,
    egress_profile: {uuid: egressUuid},
    token_sha256: createHash('sha256').update(token).digest('hex'),
    routes: [{protocol: 'vless', path: '/private-fixture'}], preferred_mode: 'auto', preferred: [],
  };
  const calls = [];
  const mf = new Miniflare(convertV4MiniflareOptions({
    modules: [{type: 'ESModule', path: 'worker.mjs'}], compatibilityDate: '2026-10-01',
    bindings: {SUB_CONFIG: JSON.stringify(config)},
    outboundService: async request => {
      calls.push({url: request.url, headers: Object.fromEntries(request.headers), body: await request.text()});
      return new Response('104.17.1.1#public source');
    },
  }));
  t.after(() => mf.dispose());
  const url = `https://sub.example.test/s/${token}/Private-XUI.yaml`;
  const response = await mf.dispatchFetch(url, {headers: {
    Authorization: `Bearer ${token}`, Cookie: `${uuid}=${egressUuid}`,
    Referer: `https://node.example.test/${uuid}/${egressUuid}`,
  }});
  assert.equal(response.status, 200);
  const body = YAML.parse(await response.text());
  assert.equal(body.proxies.length, 26);
  assert.deepEqual(body['proxy-groups'][0].proxies, ['后置 SOCKS5', '无后置']);
  for (const group of body['proxy-groups'].filter(group => group.type === 'url-test')) {
    const credentials = group.proxies.map(name => body.proxies.find(proxy => proxy.name === name).uuid);
    assert.equal(new Set(credentials).size, 1);
    assert.equal(body.proxies.find(proxy => proxy.name === group.proxies[0]).server, '104.17.1.1');
  }
  for (const [egress, expected, forbidden] of [['socks', egressUuid, uuid], ['direct', uuid, egressUuid]]) {
    const filtered = await mf.dispatchFetch(`${url}?egress=${egress}&format=raw`);
    assert.equal(filtered.status, 200);
    assert.match(filtered.headers.get('Content-Disposition'), new RegExp(`Private-XUI-${egress}\\.txt`));
    const text = await filtered.text();
    assert(text.split('\n').every(line => new URL(line).username === expected));
    assert(!text.includes(forbidden));
  }
  assert.equal(calls.length, 9);
  const outbound = JSON.stringify(calls);
  for (const secret of [uuid, egressUuid, token, config.domain, config.subscription_domain,
    config.routes[0].path]) assert(!outbound.includes(secret));
  assert(calls.every(call => call.url.startsWith('https://cf.090227.xyz/') && call.body === ''));
});
