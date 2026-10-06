// Private subscription renderer. Only fixed public address-list requests leave
// the Worker. The app never includes node credentials in those requests;
// Cloudflare may add platform metadata such as CF-Worker.
const HEADERS = {
  'Cache-Control': 'private, no-store, max-age=0',
  'CDN-Cache-Control': 'no-store',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
};

const DYNAMIC_SOURCES = Object.freeze({
  'https://cf.090227.xyz/ct?ips=12': '电信',
  'https://cf.090227.xyz/cu?ips=12': '联通',
  'https://cf.090227.xyz/cmcc?ips=12': '移动',
});
const GITHUB_SOURCE = 'https://raw.githubusercontent.com/qwer-search/bestip/refs/heads/main/kejilandbestip.txt';
const SOURCE_TIMEOUT_MS = 5000;
const MAX_SOURCE_BYTES = 65536;
const MAX_ENDPOINTS = 128;
const DOMAIN_POOL = [
  'cloudflare.182682.xyz', 'freeyx.cloudflare88.eu.org', 'bestcf.top',
  'cdn.2020111.xyz', 'cf.0sm.com', 'cf.090227.xyz', 'cf.zhetengsha.eu.org',
  'cfip.1323123.xyz', 'cloudflare-ip.mofashi.ltd', 'cf.877771.xyz', 'xn--b6gac.eu.org',
];
// Cloudflare's published networks, checked 2026-10-06:
// https://www.cloudflare.com/ips-v4/ and https://www.cloudflare.com/ips-v6/
const CF_V4 = [
  '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
  '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
  '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
  '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
];
const CF_V6 = ['2400:cb00::/32', '2606:4700::/32', '2803:f800::/32',
  '2405:b500::/32', '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32'];

function ipv4Number(address) {
  const parts = address.split('.');
  if (parts.length !== 4 || parts.some(part => !/^\d{1,3}$/.test(part) || Number(part) > 255)) return null;
  return parts.reduce((value, part) => value * 256n + BigInt(part), 0n);
}

function ipv6Number(address) {
  if (!/^[0-9a-f:]+$/i.test(address) || address.split('::').length > 2) return null;
  const [left, right] = address.split('::');
  const start = left ? left.split(':') : [];
  const end = right ? right.split(':') : [];
  const missing = 8 - start.length - end.length;
  if (right === undefined ? missing !== 0 : missing < 1) return null;
  const parts = [...start, ...Array(missing).fill('0'), ...end];
  if (parts.some(part => !/^[0-9a-f]{1,4}$/i.test(part))) return null;
  return parts.reduce((value, part) => value * 65536n + BigInt(`0x${part}`), 0n);
}

function cloudflareAddress(value) {
  const address = String(value || '').trim();
  const ipv6 = address.includes(':');
  const parse = ipv6 ? ipv6Number : ipv4Number;
  const number = parse(address);
  if (number === null) return null;
  const width = ipv6 ? 128 : 32;
  const allowed = (ipv6 ? CF_V6 : CF_V4).some(cidr => {
    const [network, bits] = cidr.split('/');
    const shift = BigInt(width - Number(bits));
    return (number >> shift) === (parse(network) >> shift);
  });
  if (!allowed) return null;
  return ipv6 ? new URL(`https://[${address}]/`).hostname.slice(1, -1)
    : address.split('.').map(Number).join('.');
}

async function publicList(url) {
  // No caller-supplied URL, headers, cookies, Referer, credentials, or body.
  if (!Object.hasOwn(DYNAMIC_SOURCES, url) && url !== GITHUB_SOURCE) {
    throw new Error('Unapproved address source');
  }
  const controller = new AbortController();
  let reader;
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      if (reader) void reader.cancel().catch(() => {});
      reject(new Error('Address source timeout'));
    }, SOURCE_TIMEOUT_MS);
  });
  try {
    return await Promise.race([timeout, (async () => {
      const response = await fetch(url, {
        // Workers rejects redirect:error before issuing the request.
        // manual plus response.ok below rejects 3xx without following them.
        method: 'GET', redirect: 'manual', credentials: 'omit',
        headers: {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json, text/plain'},
        signal: controller.signal,
      });
      if (!response.ok || Number(response.headers.get('Content-Length')) > MAX_SOURCE_BYTES || !response.body) {
        if (response.body) void response.body.cancel().catch(() => {});
        throw new Error('Address source unavailable');
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let total = 0;
      let text = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) return text + decoder.decode();
        total += value.byteLength;
        if (total > MAX_SOURCE_BYTES) throw new Error('Address source too large');
        text += decoder.decode(value, {stream: true});
      }
    })()]);
  } finally {
    clearTimeout(timer);
    controller.abort();
    if (reader) void reader.cancel().catch(() => {});
  }
}

async function carrierIPs(url, name) {
  const results = [];
  for (const line of (await publicList(url)).split(/\r?\n/)) {
    // Documented response: one IP per line, optionally followed by #label.
    // Never copy provider-controlled labels into names or accept non-CF relays.
    const address = cloudflareAddress(line.split('#')[0].trim());
    if (address) results.push({address, name: `公开优选 · ${name}`});
    if (results.length >= MAX_ENDPOINTS) break;
  }
  return results;
}

async function githubIPs() {
  const results = [];
  for (const line of (await publicList(GITHUB_SOURCE)).split(/\r?\n/)) {
    const host = line.split('#')[0].trim();
    const parts = host.match(/^(?:\[([0-9a-f:]+)\]|([\d.]+))(?::(\d+))?$/i);
    if (!parts) continue;
    const address = cloudflareAddress(parts[1] || parts[2]);
    const port = Number(parts[3] || 443);
    // This deployment uses HTTP WebSocket at the origin. Keep the edge on 443;
    // other HTTPS edge ports do not have the same Flexible SSL behavior.
    if (address && port === 443) results.push({address, name: '公开优选 · GitHub'});
    if (results.length >= MAX_ENDPOINTS) break;
  }
  return results;
}

async function automaticEndpoints(includeGithub = false) {
  // This function deliberately cannot receive subscription config or Request.
  const sources = Object.entries(DYNAMIC_SOURCES).map(([url, name]) => carrierIPs(url, name));
  if (includeGithub === true) sources.push(githubIPs());
  const settled = await Promise.allSettled(sources);
  const ips = settled.flatMap(result => result.status === 'fulfilled' ? result.value : []);
  const fallback = Array.from({length: 6}, (_, i) => ({
    address: `104.${16 + i}.0.1`, name: `CF 候选 ${String(i + 1).padStart(2, '0')}`,
  }));
  const seen = new Set();
  return [...DOMAIN_POOL.map(address => ({address, name: `优选域名 · ${address}`})), ...(ips.length ? ips : fallback)]
    .filter(endpoint => {
      const key = endpoint.address.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, MAX_ENDPOINTS - 1);
}

function reply(body, status = 200, type = 'text/plain; charset=utf-8', extra = {}) {
  return new Response(body, {status, headers: {...HEADERS, 'Content-Type': type, ...extra}});
}

function base64(value) {
  return btoa(Array.from(new TextEncoder().encode(value), byte => String.fromCharCode(byte)).join(''));
}

async function authorized(token, expected) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(token) || !/^[0-9a-f]{64}$/.test(expected || '')) return false;
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  const actual = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
  let difference = 0;
  for (let i = 0; i < 64; i++) difference |= actual.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

function nodes(config, protocol) {
  // A candidate address changes only the Cloudflare entry point; SNI/Host and
  // the origin credentials stay the same. Keep the normal DNS entry as fallback.
  const seen = new Set();
  const endpoints = [{address: config.domain, name: '域名入口'}, ...(config.preferred || [])]
    .filter(endpoint => {
      const key = endpoint.address.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  const routes = config.routes.filter(route => !protocol || route.protocol === protocol);
  return routes.flatMap(route => endpoints.map((endpoint, i) => ({
    name: `${route.protocol.toUpperCase()} · ${endpoint.name || endpoint.address} · ${i + 1}`,
    protocol: route.protocol,
    server: endpoint.address,
    port: 443,
    host: config.domain,
    path: route.path,
    credential: config.uuid,
  })));
}

function uri(node) {
  const server = node.server.includes(':') ? `[${node.server}]` : node.server;
  if (node.protocol === 'vmess') {
    return 'vmess://' + base64(JSON.stringify({
      v: '2', ps: node.name, add: node.server, port: String(node.port),
      id: node.credential, aid: '0', scy: 'auto', net: 'ws', type: 'none',
      host: node.host, path: node.path, tls: 'tls', sni: node.host,
    }));
  }
  const params = new URLSearchParams({security: 'tls', sni: node.host, type: 'ws', host: node.host, path: node.path});
  if (node.protocol === 'vless') params.set('encryption', 'none');
  return `${node.protocol}://${encodeURIComponent(node.credential)}@${server}:${node.port}?${params}#${encodeURIComponent(node.name)}`;
}

function yamlScalar(value) {
  return typeof value === 'string' ? JSON.stringify(value) : String(value);
}

function yaml(value, depth = 0) {
  const space = ' '.repeat(depth);
  if (Array.isArray(value)) {
    return value.map(item => {
      if (item !== null && typeof item === 'object') {
        const lines = yaml(item, depth + 2).split('\n');
        return `${space}- ${lines[0].slice(depth + 2)}\n${lines.slice(1).join('\n')}`.trimEnd();
      }
      return `${space}- ${yamlScalar(item)}`;
    }).join('\n');
  }
  return Object.entries(value).map(([key, item]) => {
    if (item !== null && typeof item === 'object') return `${space}${key}:\n${yaml(item, depth + 2)}`;
    return `${space}${key}: ${yamlScalar(item)}`;
  }).join('\n');
}

function clash(list) {
  // Quote every string so paths, Unicode names and credentials remain literal YAML.
  const proxies = list.map(node => ({
    name: node.name, type: node.protocol, server: node.server, port: node.port,
    ...(node.protocol === 'trojan' ? {password: node.credential} : {uuid: node.credential}),
    ...(node.protocol === 'vmess' ? {alterId: 0, cipher: 'auto'} : {}),
    tls: true, udp: true, 'skip-cert-verify': false,
    ...(node.protocol === 'trojan' ? {sni: node.host} : {servername: node.host}),
    network: 'ws', 'ws-opts': {path: node.path, headers: {Host: node.host}},
  }));
  const names = proxies.map(node => node.name);
  const groups = [{name: 'PROXY', type: 'select', proxies: names}];
  if (names.length > 1) {
    groups[0].proxies = ['自动选择', ...names];
    groups.push({
      name: '自动选择', type: 'url-test', proxies: names,
      // Measured by the client over the complete proxy path. This is a latency
      // and reachability check, not a bandwidth benchmark or a server-side claim.
      url: 'https://www.gstatic.com/generate_204', interval: 300, tolerance: 50,
    });
  }
  return yaml({
    'mixed-port': 7890, 'allow-lan': false, mode: 'rule',
    proxies,
    'proxy-groups': groups,
    rules: ['MATCH,PROXY'],
  }) + '\n';
}

function subscriptionHeaders(config, format) {
  // Keep filenames and response headers independent of the bearer token.
  // filename* is understood by Clash Verge; the ASCII fallback is for older clients.
  const title = Array.from(String(config.subscription_name || 'Private XUI')
    .replace(/[\u0000-\u001f\u007f-\u009f\\/:*?"<>|]/g, ' ').replace(/\s+/g, ' ').trim())
    .slice(0, 80).join('') || 'Private XUI';
  const extension = format === 'clash' ? 'yaml' : 'txt';
  const filename = encodeURIComponent(`${title}.${extension}`).replace(/['()*]/g,
    character => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
  return {
    'Content-Disposition': `attachment; filename="Private-XUI.${extension}"; filename*=UTF-8''${filename}`,
    'Profile-Title': `base64:${base64(title)}`,
    'Profile-Update-Interval': '24',
  };
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (url.protocol !== 'https:') return reply('HTTPS required', 400);
      if (request.method !== 'GET' && request.method !== 'HEAD') return reply('Method not allowed', 405);
      const config = JSON.parse(env.SUB_CONFIG || '{}');
      if (url.hostname !== config.subscription_domain) return reply('Not found', 404);
      const match = url.pathname.match(/^\/s\/([A-Za-z0-9_-]+)(?:\/Private-XUI\.yaml)?$/);
      const bearer = request.headers.get('Authorization') || '';
      const token = match?.[1] || (url.pathname === '/sub' && bearer.startsWith('Bearer ') ? bearer.slice(7) : '');
      if (!await authorized(token, config.token_sha256)) return reply('Not found', 404);
      for (const key of url.searchParams.keys()) {
        if (!['format', 'protocol'].includes(key)) return reply('Unsupported parameter', 400);
      }
      const format = url.searchParams.get('format') || 'clash';
      const protocol = url.searchParams.get('protocol');
      if (protocol && !['vless', 'trojan', 'vmess'].includes(protocol)) return reply('Unsupported protocol', 400);
      if (!['base64', 'raw', 'clash'].includes(format)) return reply('Unsupported format', 400);
      if (!config.routes.some(route => !protocol || route.protocol === protocol)) return reply('Protocol not configured', 404);
      const activeConfig = config.preferred_mode === 'auto'
        ? {...config, preferred: await automaticEndpoints(config.preferred_github === true)} : config;
      const list = nodes(activeConfig, protocol);
      if (!list.length) return reply('Protocol not configured', 404);
      const raw = list.map(uri).join('\n');
      const body = format === 'clash' ? clash(list) : format === 'raw' ? raw : base64(raw);
      return reply(request.method === 'HEAD' ? null : body, 200,
        format === 'clash' ? 'text/yaml; charset=utf-8' : 'text/plain; charset=utf-8',
        subscriptionHeaders(config, format));
    } catch {
      // Never return request URLs, bindings, credentials, or exception details.
      return reply('Subscription unavailable', 503);
    }
  },
};
