// 统一本地接口调用：超时控制、中文报错、断线横幅与二进制上传。
export const DEFAULT_TIMEOUT_MS = 20000;
export const LONG_TIMEOUT_MS = 60000;
export const UPLOAD_TIMEOUT_MS = 120000;

const LONG_PATTERNS = [
  '/api/scan', '/deliver', '/api/storage/cleanup',
  '/api/ai/', '/api/render-target/', '/api/render-node',
  '/api/settings/detect-max', '/api/settings/connect-max', '/api/canvas/models',
];

export function timeoutForPath(path) {
  const url = String(path || '');
  return LONG_PATTERNS.some(part => url.includes(part)) ? LONG_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
}

export function backoffDelay(failures, baseMs = 2000, capMs = 15000) {
  if (!Number.isFinite(failures) || failures <= 0) return 0;
  return Math.min(capMs, baseMs * 2 ** (failures - 1));
}

export function buildFileUrl(path, taskId = '', width = 0) {
  const params = new URLSearchParams({path});
  if (taskId) params.set('task_id', taskId);
  const w = Number(width);
  if (Number.isFinite(w) && w > 0) params.set('w', String(Math.round(w)));
  return '/api/file?' + params;
}

export async function readJsonSafe(response) {
  const text = await response.text().catch(() => '');
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}

export function formatError(response, data, fallback = '请求失败') {
  if (typeof data?.error === 'string' && data.error.trim()) return data.error;
  if (response?.status) return `${fallback}（状态码 ${response.status}）`;
  return fallback + '。';
}

export function classifyNetworkError(error, timedOut = false, cancelled = false) {
  if (cancelled) return Object.assign(new Error('请求已取消'), {code: 'aborted', aborted: true});
  if (timedOut) return Object.assign(new Error('请求超时，服务没有响应'), {code: 'timeout', offline: true});
  return Object.assign(new Error('服务没响应，请检查软件是否在运行'), {code: 'offline', offline: true, cause: error});
}

let offline = false;
let offlineMessage = '';
let retryHandler = null;
let bannerEl = null;

export function isOffline() { return offline; }
export function setOfflineRetry(fn) { retryHandler = typeof fn === 'function' ? fn : null; }

function ensureBanner() {
  if (typeof document === 'undefined' || !document.body) return null;
  if (bannerEl?.isConnected) return bannerEl;
  bannerEl = document.getElementById('offline-banner');
  if (!bannerEl) {
    bannerEl = document.createElement('div');
    bannerEl.id = 'offline-banner';
    bannerEl.className = 'offline-banner';
    bannerEl.setAttribute('role', 'alert');
    bannerEl.hidden = true;
    bannerEl.innerHTML = '<span class="offline-dot" aria-hidden="true"></span><span class="offline-text"></span><button type="button" class="offline-retry">重试</button>';
    bannerEl.querySelector('.offline-retry').onclick = () => {
      const btn = bannerEl.querySelector('.offline-retry');
      btn.disabled = true;
      Promise.resolve(retryHandler?.()).finally(() => { if (btn.isConnected) btn.disabled = false; });
    };
    document.body.append(bannerEl);
  }
  return bannerEl;
}

export function setOffline(state, message = '服务没响应，请检查软件是否在运行，正在自动重连…') {
  offline = Boolean(state);
  offlineMessage = offline ? message : '';
  const el = ensureBanner();
  if (!el) return;
  el.hidden = !offline;
  if (offline) el.querySelector('.offline-text').textContent = offlineMessage;
}

export async function fetchWithTimeout(url, init = {}, opts = {}) {
  const {timeout = DEFAULT_TIMEOUT_MS, signal, fetchImpl = globalThis.fetch, trackOffline = true} = opts;
  const controller = new AbortController();
  let timedOut = false;
  const timer = timeout > 0 ? setTimeout(() => { timedOut = true; controller.abort(); }, timeout) : null;
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) {
      clearTimeout(timer);
      throw classifyNetworkError(null, false, true);
    }
    signal.addEventListener('abort', onAbort, {once: true});
  }
  try {
    const response = await fetchImpl(url, {...init, signal: controller.signal});
    if (trackOffline) setOffline(false);
    return response;
  } catch (error) {
    if (signal?.aborted && !timedOut) throw classifyNetworkError(error, false, true);
    const wrapped = classifyNetworkError(error, timedOut, false);
    if (trackOffline) {
      setOffline(true, timedOut ? '请求超时，服务没有响应，正在自动重连…' : '服务没响应，请检查软件是否在运行，正在自动重连…');
    }
    throw wrapped;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}

export async function api(path, body, method, opts = {}) {
  const timeout = opts.timeout ?? timeoutForPath(path);
  const response = await fetchWithTimeout(path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
  }, {...opts, timeout});
  const result = await readJsonSafe(response);
  if (!response.ok || !result) {
    throw Object.assign(new Error(formatError(response, result, '请求失败')), {status: response.status});
  }
  return result;
}

export async function uploadBlob(url, blob, opts = {}) {
  const {method = 'POST', headers = {}, timeout = UPLOAD_TIMEOUT_MS, fallback = '上传失败', ...rest} = opts;
  const response = await fetchWithTimeout(url, {method, headers, body: blob}, {...rest, timeout});
  const result = await readJsonSafe(response);
  if (!response.ok) {
    throw Object.assign(new Error(formatError(response, result, fallback)), {status: response.status});
  }
  return result;
}
