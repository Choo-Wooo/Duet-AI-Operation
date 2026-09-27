/* 문서 목록을 폴더 트리로 만든다 (DOM 없음, node 로 테스트 가능).
 * rows: [{path, mtime, size}]  →  화면에 보일 줄 목록 [{key, kind, depth, name, path, count, open, mtime}]
 * - 폴더는 접고 펼칠 수 있다. 접힘 상태는 state[폴더경로] = true(펼침)/false(접힘), 없으면 기본값.
 * - 기본값: 최상위 폴더만 펼침, 선택한 문서가 들어 있는 폴더는 항상 펼침.
 * - 검색어가 있으면 맞는 문서와 그 상위 폴더만 모두 펼쳐 보여 준다.
 * - 맨 위에 '최근 수정' 묶음(최신 N개)을 둔다. 이것도 접을 수 있다(키 '@recent').
 */
(function(root) {
  'use strict';
  const RECENT = '@recent';
  const collator = typeof Intl !== 'undefined' ? new Intl.Collator('ko', {numeric: true, sensitivity: 'base'}) : null;
  const cmp = (a, b) => collator ? collator.compare(a, b) : (a < b ? -1 : a > b ? 1 : 0);
  // 최상위 순서: 루트 문서 → docs → 그 밖의 폴더 → .duet
  const topRank = name => name === 'docs' ? 0 : name.startsWith('.') ? 2 : 1;

  function build(rows, opts) {
    opts = opts || {};
    const state = opts.state || {};
    const query = (opts.query || '').trim().toLowerCase();
    const selected = opts.selected || null;
    const recentN = opts.recent === undefined ? 8 : opts.recent;
    const list = query ? rows.filter(r => r.path.toLowerCase().includes(query)) : rows.slice();

    // 트리 구성
    const tree = {dirs: new Map(), files: [], count: 0, path: ''};
    for (const r of list) {
      const parts = r.path.split('/');
      let node = tree;
      node.count++;
      for (let i = 0; i < parts.length - 1; i++) {
        const p = parts.slice(0, i + 1).join('/');
        if (!node.dirs.has(parts[i])) node.dirs.set(parts[i], {dirs: new Map(), files: [], count: 0, path: p, name: parts[i]});
        node = node.dirs.get(parts[i]);
        node.count++;
      }
      node.files.push(r);
    }
    const selectedDirs = new Set();
    if (selected) {
      const parts = selected.split('/');
      for (let i = 1; i < parts.length; i++) selectedDirs.add(parts.slice(0, i).join('/'));
    }
    const isOpen = (path, depth) => {
      if (query) return true;
      if (selectedDirs.has(path) && state[path] !== false) return true;
      if (path in state) return !!state[path];
      return depth === 0;
    };

    const out = [];
    // 최근 수정
    if (!query && recentN > 0 && rows.length > recentN) {
      const recent = rows.slice().sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, recentN);
      const open = RECENT in state ? !!state[RECENT] : true;
      out.push({key: 'd:' + RECENT, kind: 'folder', depth: 0, name: '최근 수정', path: RECENT, count: recent.length, open});
      if (open) for (const r of recent) out.push({key: 'r:' + r.path, kind: 'file', depth: 1, name: r.path.split('/').pop(),
        dir: r.path.includes('/') ? r.path.slice(0, r.path.lastIndexOf('/')) : '', path: r.path, mtime: r.mtime, recent: true});
    }
    const walk = (node, depth) => {
      const dirs = [...node.dirs.values()].sort((a, b) =>
        depth === 0 ? (topRank(a.name) - topRank(b.name) || cmp(a.name, b.name)) : cmp(a.name, b.name));
      const files = node.files.slice().sort((a, b) =>
        depth === 0 ? ((a.path === 'DIALOGUE.md' ? -1 : 0) - (b.path === 'DIALOGUE.md' ? -1 : 0) || cmp(a.path, b.path))
                    : cmp(a.path, b.path));
      if (depth === 0) {  // 루트 문서(DIALOGUE.md 등)는 폴더보다 먼저
        for (const f of files) out.push({key: 'f:' + f.path, kind: 'file', depth, name: f.path.split('/').pop(), path: f.path, mtime: f.mtime});
      }
      for (const d of dirs) {
        const open = isOpen(d.path, depth);
        out.push({key: 'd:' + d.path, kind: 'folder', depth, name: d.name, path: d.path, count: d.count, open});
        if (open) walk(d, depth + 1);
      }
      if (depth > 0) {
        for (const f of files) out.push({key: 'f:' + f.path, kind: 'file', depth, name: f.path.split('/').pop(), path: f.path, mtime: f.mtime});
      }
    };
    walk(tree, 0);
    return out;
  }

  // 모든 폴더 경로 (모두 펼치기/접기용)
  function folders(rows) {
    const set = new Set();
    for (const r of rows) {
      const parts = r.path.split('/');
      for (let i = 1; i < parts.length; i++) set.add(parts.slice(0, i).join('/'));
    }
    return [...set];
  }

  function shortDate(mtime) {
    if (!mtime) return '';
    const d = new Date(mtime * 1000);
    const p = n => String(n).padStart(2, '0');
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  const api = {build, folders, shortDate, RECENT};
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.DocTree = api;
})(typeof window !== 'undefined' ? window : null);
