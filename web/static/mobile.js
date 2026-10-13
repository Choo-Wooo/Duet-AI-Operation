/* Mobile navigation uses the desktop controls, so there is one source of state. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DuetMobile = api.mount(root.document, root);
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const MORE_VIEWS = {ask: '질문', sessions: '세션', roles: '역할·모델', settings: '설정'};

  function viewportState(innerHeight, viewport, editing) {
    // Pinch zoom must remain usable; it is not a software keyboard resize.
    const usable = viewport && Math.abs((viewport.scale || 1) - 1) < .05;
    const height = usable ? viewport.height : innerHeight;
    const top = usable ? Math.max(0, viewport.offsetTop || 0) : 0;
    return {height: Math.max(1, Math.round(height)), top, bottom: Math.max(0, innerHeight - height - top),
      keyboard: !!(usable && editing && innerHeight - height > 100)};
  }

  function mount(doc, win) {
    const $ = selector => doc.querySelector(selector);
    const media = win.matchMedia('(max-width: 860px)');
    const more = $('#mobileMoreDialog'), moreButton = $('#mobileMoreButton');
    const panelTabs = [...doc.querySelectorAll('[data-live-panel]')];
    const mobileButtons = [...doc.querySelectorAll('[data-mobile-view]')];
    const moved = ['conversationMode', 'designQuestionsBadge', 'autopilot', 'turns', 'cost', 'ctx'].map(id => {
      const node = $('#' + id), anchor = doc.createComment('mobile-home:' + id);
      node.parentNode.insertBefore(anchor, node);
      return {node, anchor};
    });
    let currentView = 'live', frame = 0;

    function closeMore() { if (more.open) more.close(); }
    function viewChanged(view) {
      currentView = view;
      mobileButtons.forEach(button => {
        if (button.dataset.mobileView === view) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      });
      if (MORE_VIEWS[view]) moreButton.setAttribute('aria-current', 'page');
      else moreButton.removeAttribute('aria-current');
      $('#mobileMoreLabel').textContent = MORE_VIEWS[view] || '더보기';
      closeMore();
    }
    mobileButtons.forEach(button => button.addEventListener('click', () => {
      const target = $('#nav button[data-v="' + button.dataset.mobileView + '"]');
      if (target) target.click();
    }));
    moreButton.addEventListener('click', () => { if (!more.open) more.showModal(); });
    $('#mobileMoreClose').addEventListener('click', closeMore);
    more.addEventListener('click', event => { if (event.target === more) closeMore(); });
    // The auto-accept chip retains its existing click handler after moving.
    $('#autopilot').addEventListener('click', closeMore);

    function setPanel(panel, focus) {
      if (!['flow', 'stream'].includes(panel)) return;
      $('.live').dataset.mobilePanel = panel;
      panelTabs.forEach(tab => {
        const active = tab.dataset.livePanel === panel;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        if (active && focus) tab.focus();
      });
      // Desktop keeps both panels visible and does not expose tabpanel semantics.
      for (const name of ['flow', 'stream']) {
        const section = $('#' + name + 'Panel');
        if (media.matches) {
          section.setAttribute('role', 'tabpanel');
          section.setAttribute('aria-labelledby', name === 'flow' ? 'mobileFlowTab' : 'mobileStreamTab');
        } else {
          section.removeAttribute('role');
          section.removeAttribute('aria-labelledby');
        }
      }
    }
    panelTabs.forEach(tab => {
      tab.addEventListener('click', () => setPanel(tab.dataset.livePanel));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        setPanel(event.key === 'Home' ? 'flow' : event.key === 'End' ? 'stream' : tab.dataset.livePanel === 'flow' ? 'stream' : 'flow', true);
      });
    });
    $('#mobileDocBack').addEventListener('click', () => {
      $('#v-docs').classList.remove('mobile-document-open');
      const selected = $('#docList .sel');
      if (selected) selected.focus({preventScroll: true});
    });
    function openDocument() {
      $('#v-docs').classList.add('mobile-document-open');
      if (media.matches) $('#mobileDocBack').focus({preventScroll: true});
    }

    const inputs = ['#input', '#askInput'].map(selector => $(selector));
    const placeholders = inputs.map(input => input.placeholder);
    inputs.forEach(input => input.addEventListener('keydown', event => {
      // A phone keyboard's Return key inserts a line; explicit Send sends it.
      if (media.matches && event.key === 'Enter' && !event.ctrlKey && !event.metaKey) event.stopImmediatePropagation();
    }, true));

    function updateViewport() {
      frame = 0;
      const focused = doc.activeElement;
      const editing = !!(focused && /^(INPUT|TEXTAREA|SELECT)$/.test(focused.tagName));
      const state = viewportState(win.innerHeight, win.visualViewport, editing);
      if (media.matches) {
        doc.documentElement.style.setProperty('--mobile-viewport-h', state.height + 'px');
        doc.documentElement.style.setProperty('--mobile-viewport-top', state.top + 'px');
        doc.documentElement.style.setProperty('--mobile-viewport-bottom', state.bottom + 'px');
      } else {
        doc.documentElement.style.removeProperty('--mobile-viewport-h');
        doc.documentElement.style.removeProperty('--mobile-viewport-top');
        doc.documentElement.style.removeProperty('--mobile-viewport-bottom');
      }
      doc.documentElement.classList.toggle('keyboard-open', media.matches && state.keyboard);
    }
    function scheduleViewport() { if (!frame) frame = win.requestAnimationFrame(updateViewport); }
    function measureTray() {
      const tray = $('#pendingTray');
      const height = media.matches && !tray.classList.contains('zero') ? tray.offsetHeight : 0;
      doc.documentElement.style.setProperty('--mobile-tray-space', (height ? height + 24 : 0) + 'px');
    }
    function measureHeader() {
      const height = media.matches ? $('.app > header').offsetHeight : 0;
      doc.documentElement.style.setProperty('--mobile-header-h', (height || 0) + 'px');
    }
    function layoutChanged() {
      const compact = media.matches;
      for (const {node, anchor} of moved) {
        if (compact) $('#mobileSessionOptions').appendChild(node);
        else anchor.parentNode.insertBefore(node, anchor.nextSibling);
      }
      inputs.forEach((input, i) => { input.placeholder = compact ? (i ? '프로젝트에 대해 물어보세요' : '메시지를 입력하세요') : placeholders[i]; });
      if (!compact) closeMore();
      setPanel($('.live').dataset.mobilePanel || 'flow');
      viewChanged(currentView);
      measureHeader();
      measureTray();
      scheduleViewport();
    }
    if (media.addEventListener) media.addEventListener('change', layoutChanged);
    else media.addListener(layoutChanged);
    win.addEventListener('resize', scheduleViewport);
    if (win.visualViewport) {
      win.visualViewport.addEventListener('resize', scheduleViewport);
      win.visualViewport.addEventListener('scroll', scheduleViewport);
    }
    doc.addEventListener('focusin', scheduleViewport);
    doc.addEventListener('focusout', scheduleViewport);
    if (win.ResizeObserver) {
      new win.ResizeObserver(measureHeader).observe($('.app > header'));
      new win.ResizeObserver(measureTray).observe($('#pendingTray'));
    }
    layoutChanged();
    return {viewChanged, openDocument};
  }
  return {mount, viewportState};
});
