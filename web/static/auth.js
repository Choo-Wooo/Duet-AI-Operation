/* A URL token bootstraps trust; only a verified HttpOnly cookie replaces it. */
(function (root) {
  'use strict';
  class AuthError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }
  function create(env) {
    let token = new URL(env.location.href).searchParams.get('t') || '';
    let trusted = false;
    async function request(path, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      try {
        return await env.fetch(path, {...options, credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      } finally { clearTimeout(timer); }
    }
    async function authorize() {
      const res = await request('/api/session');
      // Static assets can update while an older server is still running.
      // Verify its existing authentication without claiming persistent trust.
      if (res.status === 404) {
        const legacy = await request(pathWithToken('/api/state'));
        if (legacy.ok) { trusted = false; return {authenticated: true, trusted: false, legacy: true}; }
        throw new AuthError('서버 접속을 확인하지 못했습니다. 터미널의 토큰 주소로 다시 접속하세요.', legacy.status);
      }
      if (res.ok) {
        const session = await res.json();
        if (!session.authenticated || !session.trusted) throw new AuthError('브라우저 등록을 확인하지 못했습니다.', 503);
        trusted = true;
        token = '';
        const url = new URL(env.location.href);
        if (url.searchParams.has('t')) {
          url.searchParams.delete('t');
          env.history.replaceState(env.history.state, '', url.pathname + url.search + url.hash);
        }
        return session;
      }
      // A browser blocking cookies can still use its original token URL.
      // Do not discard that URL, or persist the credential in web storage.
      if (res.status === 403 && token) {
        const fallback = await request('/api/state?t=' + encodeURIComponent(token));
        if (fallback.ok) { trusted = false; return {authenticated: true, trusted: false}; }
        throw new AuthError('접속 링크가 만료되었습니다. 터미널의 새 토큰 주소로 접속하세요.', fallback.status);
      }
      trusted = false;
      throw new AuthError(res.status === 403 ? '이 브라우저의 접속 등록이 만료되었거나 해제되었습니다. 터미널의 토큰 주소로 다시 접속하세요.' : '브라우저 등록을 확인할 수 없습니다. 잠시 후 다시 연결합니다.', res.status);
    }
    function pathWithToken(path) {
      if (!token) return path;
      const url = new URL(path, env.location.href);
      url.searchParams.set('t', token);
      return url.pathname + url.search;
    }
    async function json(path, options) {
      const res = await request(pathWithToken(path), options);
      if (!res.ok) throw new AuthError(res.status === 403 ? '접속 권한을 확인할 수 없습니다. 토큰 주소로 다시 접속하세요.' : '요청을 처리하지 못했습니다 (' + res.status + ').', res.status);
      return res.json();
    }
    return {
      authorize, json,
      socketURL() { return (env.location.protocol === 'https:' ? 'wss://' : 'ws://') + env.location.host + pathWithToken('/ws'); },
      forget() { token = ''; trusted = false; },
      get trusted() { return trusted; }
    };
  }
  root.DuetAuth = {create, AuthError};
})(typeof module === 'object' && module.exports ? module.exports : window);
