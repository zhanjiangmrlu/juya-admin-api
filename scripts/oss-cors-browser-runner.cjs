// Isolated, read-only Chromium CORS check. Input signatures remain in memory.
const fs = require('node:fs');
const { createRequire } = require('node:module');
const fromAdmin = createRequire(require('node:path').join(__dirname, '../../juya-admin/package.json'));
let chromium;
try {
  ({ chromium } = fromAdmin('@playwright/test'));
  if (process.argv.includes('--check-dependencies')) {
    if (!fs.existsSync(chromium.executablePath())) throw new Error('Chromium unavailable');
    console.log(JSON.stringify({ ready: true }));
    process.exit(0);
  }
} catch (error) {
  console.log(JSON.stringify({ skipped: 'Sibling admin Playwright dependencies or Chromium unavailable' }));
  process.exit(77);
}

async function main() {
  // 功能: 从标准输入读取已有资源的签名, 逐来源检查浏览器 GET/HEAD 和真实预检响应。
  // 参数: 无; origins、getUrl、headUrl 和 sha256 由标准输入 JSON 提供。
  // 返回: Promise<void>, 结果以 JSON 输出, 浏览器在 finally 中关闭。
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const origin of input.origins) {
      const context = await browser.newContext();
      const page = await context.newPage();
      const cdp = await context.newCDPSession(page);
      await cdp.send('Network.enable');
      const pending = new Map();
      const preflights = [];
      cdp.on('Network.requestWillBeSent', ({ requestId, request }) => {
        // 功能: 记录 OPTIONS 预检请求, 按 CDP 请求标识关联后续响应。
        // 参数: requestId 是请求关联标识; request 包含方法、URL 和请求头。
        // 返回: 无, 将预检请求事实存入 pending。
        if (request.method !== 'OPTIONS') return;
        // 匿名函数: 将请求头名称转为小写, 方便按标准字段读取。
        // 参数: key 是请求头名称; value 是对应请求头值。
        // 返回: 包含小写名称和原值的二元数组。
        const headers = Object.fromEntries(Object.entries(request.headers).map(([key, value]) => [key.toLowerCase(), value]));
        pending.set(requestId, { method: 'OPTIONS',
          requested_method: headers['access-control-request-method'],
          requested_headers: headers['access-control-request-headers'] || null });
      });
      cdp.on('Network.responseReceived', ({ requestId, response }) => {
        // 功能: 为已记录的 OPTIONS 请求补充 HTTP 状态和跨域响应来源。
        // 参数: requestId 是请求关联标识; response 包含状态码及响应头。
        // 返回: 无, 将关联后的预检响应追加到 preflights。
        if (!pending.has(requestId)) return;
        // 匿名函数: 将响应头名称转为小写, 方便读取跨域字段。
        // 参数: key 是响应头名称; value 是对应响应头值。
        // 返回: 包含小写名称和原值的二元数组。
        const headers = Object.fromEntries(Object.entries(response.headers).map(([key, value]) => [key.toLowerCase(), value]));
        preflights.push({ ...pending.get(requestId), status: response.status,
          allow_origin: headers['access-control-allow-origin'] || null });
      });
      await page.goto(origin, { waitUntil: 'domcontentloaded' });
      const result = await page.evaluate(async ({ getUrl, headUrl, sha256 }) => {
        // 功能: 在当前网页来源读取已有对象并检查哈希、可见响应头及 HEAD 预检。
        // 参数: getUrl 是 GET 签名地址; headUrl 是 HEAD 签名地址; sha256 是预期内容哈希。
        // 返回: Promise, 解析为包含实际来源及 GET/HEAD 检查事实的对象。
        const read = { actual_origin: location.origin };
        try {
          const response = await fetch(getUrl, { signal: AbortSignal.timeout(20000) });
          const bytes = await response.arrayBuffer();
          // 匿名函数: 将 SHA-256 摘要的各字节转换为固定两位的十六进制文本。
          // 参数: value 是摘要中的一个字节, 范围为 0 到 255。
          // 返回: 补零后的两位十六进制字符串。
          const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
            .map((value) => value.toString(16).padStart(2, '0')).join('');
          read.get = { status: response.status, bytes: bytes.byteLength, hash_match: hash === sha256,
            etag_exposed: Boolean(response.headers.get('etag')) };
        } catch (error) { read.get = { blocked: true, error_type: error.name }; }
        try {
          // This non-safelisted Content-Type forces a real HEAD preflight.
          const response = await fetch(headUrl, { method: 'HEAD',
            headers: { 'Content-Type': 'application/octet-stream' }, signal: AbortSignal.timeout(20000) });
          read.head = { status: response.status, etag_exposed: Boolean(response.headers.get('etag')) };
        } catch (error) { read.head = { blocked: true, error_type: error.name }; }
        return read;
      }, input);
      results.push({ ...result, preflights });
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify({ read_only: true, synthetic_existing_fixture: true, results }));
}

// 匿名函数: 捕获浏览器检查失败, 输出错误类型并设置失败退出码。
// 参数: error 是 main 返回的 Promise 拒绝原因, 不输出其可能包含签名的消息。
// 返回: 无, 设置 process.exitCode 为 1。
main().catch((error) => { console.log(JSON.stringify({ failed: true, error_type: error.constructor.name })); process.exitCode = 1; });
