// Private subscription renderer. No outbound requests, analytics, or converters.
const HEADERS = {
  'Cache-Control': 'private, no-store, max-age=0',
  'CDN-Cache-Control': 'no-store',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
};

function reply(body, status = 200, type = 'text/plain; charset=utf-8') {
  return new Response(body, {status, headers: {...HEADERS, 'Content-Type': type}});
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

export function nodes(config, protocol) {
  const endpoints = [{address: config.domain, name: '直连域名'}, ...(config.preferred || [])];
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

export function uri(node) {
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

export function clash(list) {
  // Quote every string so paths, Unicode names and credentials remain literal YAML.
  const proxies = list.map(node => ({
    name: node.name, type: node.protocol, server: node.server, port: node.port,
    ...(node.protocol === 'trojan' ? {password: node.credential} : {uuid: node.credential}),
    ...(node.protocol === 'vmess' ? {alterId: 0, cipher: 'auto'} : {}),
    tls: true, udp: true, 'skip-cert-verify': false,
    ...(node.protocol === 'trojan' ? {sni: node.host} : {servername: node.host}),
    network: 'ws', 'ws-opts': {path: node.path, headers: {Host: node.host}},
  }));
  return yaml({
    'mixed-port': 7890, 'allow-lan': false, mode: 'rule',
    proxies,
    'proxy-groups': [{name: 'PROXY', type: 'select', proxies: proxies.map(node => node.name)}],
    rules: ['MATCH,PROXY'],
  }) + '\n';
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (url.protocol !== 'https:') return reply('HTTPS required', 400);
      if (request.method !== 'GET' && request.method !== 'HEAD') return reply('Method not allowed', 405);
      const config = JSON.parse(env.SUB_CONFIG || '{}');
      if (url.hostname !== config.subscription_domain) return reply('Not found', 404);
      const match = url.pathname.match(/^\/s\/([A-Za-z0-9_-]+)$/);
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
      const list = nodes(config, protocol);
      if (!list.length) return reply('Protocol not configured', 404);
      const raw = list.map(uri).join('\n');
      const body = format === 'clash' ? clash(list) : format === 'raw' ? raw : base64(raw);
      return reply(request.method === 'HEAD' ? null : body, 200,
        format === 'clash' ? 'text/yaml; charset=utf-8' : 'text/plain; charset=utf-8');
    } catch {
      // Never return request URLs, bindings, credentials, or exception details.
      return reply('Subscription unavailable', 503);
    }
  },
};
