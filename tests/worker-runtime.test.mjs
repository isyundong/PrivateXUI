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
  assert(body.proxies.every(proxy=>proxy.uuid===config.uuid));
  assert.deepEqual(body['proxy-groups'].find(group=>group.name==='GLOBAL').proxies,['PROXY',...body.proxies.map(proxy=>proxy.name)]);
  const automatic=body['proxy-groups'].find(group=>group.type==='url-test');
  assert(body.proxies.find(proxy=>proxy.name===automatic.proxies[0]).name.includes('公开优选'));
  assert(!Object.hasOwn(body.tun,'route-address'));
  for(const value of ['socks','direct','all'])assert.equal((await mf.dispatchFetch(`${url}?egress=${value}`)).status,400);
  redirect=true;const fallback=YAML.parse(await(await mf.dispatchFetch(url)).text());assert.equal(fallback.proxies.length,18);
  assert.equal(calls.length,6);assert(calls.every(u=>u.startsWith('https://cf.090227.xyz/')));
});
