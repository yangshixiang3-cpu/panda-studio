import assert from 'node:assert/strict';
import test from 'node:test';
import {
  DEFAULT_TIMEOUT_MS,
  LONG_TIMEOUT_MS,
  api,
  backoffDelay,
  buildFileUrl,
  formatError,
  isOffline,
  readJsonSafe,
  setOffline,
  timeoutForPath,
  uploadBlob,
} from '../../web/api.js';

test('缩略图地址构建只在传入正宽度时附带 w 参数', () => {
  assert.equal(buildFileUrl('D:/a/1.jpg'), '/api/file?path=D%3A%2Fa%2F1.jpg');
  assert.equal(buildFileUrl('D:/a/1.jpg', 't1'), '/api/file?path=D%3A%2Fa%2F1.jpg&task_id=t1');
  assert.equal(buildFileUrl('D:/a/1.jpg', 't1', 160), '/api/file?path=D%3A%2Fa%2F1.jpg&task_id=t1&w=160');
  assert.equal(buildFileUrl('D:/a/1.jpg', '', 359.6), '/api/file?path=D%3A%2Fa%2F1.jpg&w=360');
  assert.equal(buildFileUrl('D:/a/1.jpg', '', 0), '/api/file?path=D%3A%2Fa%2F1.jpg');
});

test('断线退避与接口默认超时按路径区分', () => {
  assert.equal(backoffDelay(0), 0);
  assert.equal(backoffDelay(1), 2000);
  assert.equal(backoffDelay(2), 4000);
  assert.equal(backoffDelay(3), 8000);
  assert.equal(backoffDelay(4), 15000);
  assert.equal(backoffDelay(10), 15000);
  assert.equal(timeoutForPath('/api/tasks/123'), DEFAULT_TIMEOUT_MS);
  assert.equal(timeoutForPath('/api/scan'), LONG_TIMEOUT_MS);
  assert.equal(timeoutForPath('/api/tasks/123/deliver'), LONG_TIMEOUT_MS);
});

test('安全解析 JSON 与中文兜底错误提示', async () => {
  const htmlResp = new Response('<html>500 Internal Server Error</html>', {status: 500});
  assert.equal(await readJsonSafe(htmlResp), null);
  assert.equal(formatError({status: 500}, null, '上传失败'), '上传失败（状态码 500）');
  assert.equal(formatError({status: 400}, {error: '图片格式无效'}, '上传失败'), '图片格式无效');
  const jsonResp = new Response(JSON.stringify({ok: true}), {status: 200});
  assert.deepEqual(await readJsonSafe(jsonResp), {ok: true});
});

test('api() 超时与断网返回中文提示，恢复后清除离线状态', async () => {
  setOffline(false);
  const hangingFetch = (_url, {signal}) => new Promise((_res, rej) => {
    signal.addEventListener('abort', () => rej(new DOMException('Aborted', 'AbortError')));
  });
  await assert.rejects(
    api('/api/tasks/1', undefined, 'GET', {timeout: 25, fetchImpl: hangingFetch}),
    err => err.message === '请求超时，服务没有响应' && err.offline === true && err.code === 'timeout'
  );
  assert.equal(isOffline(), true);

  const downFetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(
    api('/api/tasks/1', undefined, 'GET', {fetchImpl: downFetch}),
    err => err.message === '服务没响应，请检查软件是否在运行' && err.offline === true
  );
  assert.equal(isOffline(), true);

  const okFetch = async () => new Response(JSON.stringify({ok: 1}), {status: 200});
  const data = await api('/api/health', undefined, 'GET', {fetchImpl: okFetch});
  assert.deepEqual(data, {ok: 1});
  assert.equal(isOffline(), false);
});

test('uploadBlob() 遇到非 JSON 500 响应给出中文错误而不报 Unexpected token', async () => {
  const html500 = async () => new Response('<html><body>500</body></html>', {status: 500});
  await assert.rejects(
    uploadBlob('/api/refs/upload', new Blob(['abc']), {fetchImpl: html500, fallback: '图片上传失败'}),
    err => err.message === '图片上传失败（状态码 500）' && err.status === 500
  );
  const json413 = async () => new Response(JSON.stringify({error: '单张图片不能超过 50 MB。'}), {status: 413});
  await assert.rejects(
    uploadBlob('/api/refs/upload', new Blob(['abc']), {fetchImpl: json413, fallback: '图片上传失败'}),
    err => err.message === '单张图片不能超过 50 MB。' && err.status === 413
  );
  const ok201 = async () => new Response(JSON.stringify({entry: {id: 'r1'}}), {status: 201});
  assert.deepEqual(await uploadBlob('/api/refs/upload', new Blob(['abc']), {fetchImpl: ok201}), {entry: {id: 'r1'}});
});
