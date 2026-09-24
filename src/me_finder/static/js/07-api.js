/* 统一请求出口：前端所有到本机后端的 HTTP 请求都经这里发出。
   - MEFinderApi.fetch：与原生 fetch 签名、返回值完全一致，只做收口；
     需要自己看 status / 读文本 / 传二进制（分片上传）的调用点用它，行为不变。
   - MEFinderApi.requestJSON / getJSON / postJSON：解析 JSON，
     !resp.ok 或 data.error 时抛出带 status / code / payload 的 Error，
     message 取后端的中文 error。
   其他文件禁止直接调用原生 fetch（test_frontend_assets 棘轮守卫）。 */
(function (global) {  // module: 07-api.js
  function request(url, options) {
    return fetch(url, options);
  }

  async function requestJSON(url, options) {
    var response = await request(url, options);
    var data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok || (data && data.error)) {
      var error = new Error((data && data.error) || '请求失败');
      error.status = response.status;
      error.code = (data && data.code) || '';
      error.payload = data;
      throw error;
    }
    return data;
  }

  function getJSON(url, options) {
    return requestJSON(url, Object.assign({cache: 'no-store'}, options || {}));
  }

  function postJSON(url, payload, options) {
    return requestJSON(url, Object.assign({
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(payload || {})
    }, options || {}));
  }

  global.MEFinderApi = Object.freeze({
    fetch: request,
    requestJSON: requestJSON,
    getJSON: getJSON,
    postJSON: postJSON
  });
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
