/* 登录 / 会员体系前端公共库
 *
 * 提供：
 *   Auth.load()/ready        —— 读取当前登录态与功能开关（/api/auth/me）
 *   Auth.can(feature)        —— 当前用户是否具备某功能（未启用登录时恒为 true）
 *   Auth.mount()             —— 渲染用户信息条、免费版提示条、给页面功能加锁
 *   Auth.applyGates(root)    —— 扫描 [data-feature] / [data-feature-lock] 并加锁
 *   Auth.request()/logout()/redeem()/changePassword()
 *
 * 页面接入方式（只改两处）：
 *   1) <script src="auth.js"></script>
 *   2) <script>Auth.mount({ into: '.page-heading .actions' });</script>
 * 功能加锁：给按钮/输入框加 data-feature="live.start"，
 *          给整块区域加 data-feature-lock="product.write"。
 */
(function (global) {
  'use strict';

  const STYLE_ID = 'auth-style';

  const state = {
    loaded: false,
    auth_enabled: true,
    authenticated: false,
    user: null,
    entitlements: {},
    plans: {},
    features: {},
    config: {},
    allow_register: true,
    must_change_password: false,
  };

  const listeners = [];
  const lockedFeatureText = {};

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function toastError(title, message) {
    if (global.LT && LT.Toast) LT.Toast.error(title, message);
    else console.warn(title, message);
  }

  function toastSuccess(title, message) {
    if (global.LT && LT.Toast) LT.Toast.success(title, message);
    else console.log(title, message);
  }

  // ───────────────────────── 样式注入 ─────────────────────────

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      .auth-bar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
      .auth-user { display:inline-flex; align-items:center; gap:8px; padding:6px 12px;
        background:var(--surface-sunken,#f4f6fb); border:1px solid var(--border,#dfe4ee);
        border-radius:var(--radius-pill,999px); font-size:13px; font-weight:600;
        color:var(--text-primary,#111c35); }
      .auth-user i { color:var(--primary,#2f6fed); }
      .auth-plan { padding:2px 8px; border-radius:var(--radius-pill,999px); font-size:11px;
        font-weight:700; background:var(--primary-light,#e8f0ff); color:var(--primary-dark,#1b4bb8); }
      .auth-plan.is-free { background:var(--surface-sunken,#eef1f7); color:var(--text-secondary,#5a6579); }
      .auth-plan.is-admin { background:#fff4e5; color:#9a5b00; }
      .auth-plan.is-expired { background:var(--danger-bg,#fdecec); color:var(--danger-strong,#b3261e); }
      .auth-banner { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
        margin:0 0 var(--space-4,16px); padding:14px 18px; border-radius:var(--radius-sm,10px);
        border:1px solid var(--primary-border,#c9dcff); border-left:4px solid var(--primary,#2f6fed);
        background:var(--primary-light,#f2f7ff); color:var(--text-primary,#111c35); }
      .auth-banner.is-warn { border-color:var(--warning-bg,#fdf0d5); border-left-color:var(--warning,#e8a33d);
        background:var(--warning-bg,#fdf7e8); }
      .auth-banner b { display:block; font-size:14px; }
      .auth-banner .hint { margin:2px 0 0; }
      .auth-banner .auth-banner-actions { margin-left:auto; display:flex; gap:8px; flex-wrap:wrap; }
      [data-feature].feature-locked, [data-feature-lock] .feature-locked {
        opacity:.5; cursor:not-allowed !important; }
      .feature-lockable { position:relative; }
      .feature-lock-overlay { position:absolute; inset:0; z-index:6; display:flex;
        align-items:center; justify-content:center; border-radius:inherit;
        background:rgba(17,28,53,.05); cursor:not-allowed; }
      .feature-lock-chip { display:inline-flex; align-items:center; gap:8px; padding:8px 16px;
        background:rgba(17,28,53,.9); color:#fff; border-radius:var(--radius-pill,999px);
        font-size:13px; font-weight:600; box-shadow:0 6px 18px rgba(17,28,53,.22); cursor:pointer; }
      .feature-lock-chip:hover { background:#111c35; }
      .auth-modal-backdrop { position:fixed; inset:0; z-index:10000; display:flex;
        align-items:center; justify-content:center; padding:20px; background:rgba(17,28,53,.5); }
      .auth-modal { width:100%; max-width:420px; background:var(--card-bg,#fff);
        border-radius:var(--radius-md,14px); box-shadow:0 20px 60px rgba(17,28,53,.3); overflow:hidden; }
      .auth-modal-head { display:flex; align-items:center; justify-content:space-between;
        padding:16px 20px; border-bottom:1px solid var(--border,#dfe4ee); font-weight:700; }
      .auth-modal-body { padding:20px; display:flex; flex-direction:column; gap:14px; }
      .auth-modal-foot { display:flex; gap:10px; justify-content:flex-end; padding:0 20px 20px; }
      /* 密码不合规要看得见：提示文字默认是灰色小字，容易被忽略 */
      .auth-modal .hint.is-error { color:#d64545; font-weight:600; }
      @media (max-width: 640px) { .auth-banner .auth-banner-actions { margin-left:0; width:100%; } }
    `;
    document.head.appendChild(style);
  }

  // ───────────────────────── 数据请求 ─────────────────────────

  async function parse(response) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    return payload || {};
  }

  async function request(url, options) {
    const opts = options || {};
    const response = await fetch(url, {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: 'same-origin',
    });
    const payload = await parse(response);

    if (response.status === 401) {
      if (opts.silent !== true) {
        const next = encodeURIComponent(location.pathname + location.search);
        location.href = '/login.html?next=' + next;
      }
      const error = new Error(payload.msg || '请先登录');
      error.code = 401;
      throw error;
    }
    if (payload.code !== 0) {
      const error = new Error(payload.msg || '操作失败');
      error.code = payload.code || response.status;
      error.payload = payload;
      if (opts.silent !== true) {
        if (response.status === 403) {
          toastError('需要升级会员', error.message);
          if (payload.upgrade_url && opts.openUpgrade !== false) openUrl(payload.upgrade_url);
        } else {
          toastError('操作失败', error.message);
        }
      }
      throw error;
    }
    return payload.data === undefined ? payload : payload.data;
  }

  async function load(force) {
    if (state.loaded && !force) return state;
    const data = await request('/api/auth/me', { silent: true });
    Object.assign(state, {
      loaded: true,
      auth_enabled: data.auth_enabled !== false,
      authenticated: !!data.authenticated,
      user: data.user || null,
      entitlements: data.entitlements || {},
      plans: data.plans || {},
      features: data.features || {},
      config: data.config || {},
      allow_register: data.allow_register !== false,
      must_change_password: !!data.must_change_password,
    });
    listeners.forEach((callback) => {
      try { callback(state); } catch (error) { console.warn(error); }
    });
    return state;
  }

  function openUrl(url) {
    const opened = global.open(url, '_blank', 'noopener');
    if (!opened) location.href = url;
  }

  // ───────────────────────── 权限判定 ─────────────────────────

  function can(feature) {
    if (!state.auth_enabled) return true;
    if (!feature) return true;
    if (!state.authenticated) return false;
    return state.entitlements[feature] === true;
  }

  function featureLabel(feature) {
    const meta = state.features[feature];
    return (meta && meta.label) || '该功能';
  }

  function isAdmin() {
    return !!(state.user && state.user.is_admin);
  }

  function isPaid() {
    return !state.auth_enabled || !!(state.user && state.user.is_paid);
  }

  function planLabel() {
    if (!state.auth_enabled) return '未启用登录';
    if (!state.user) return '未登录';
    if (state.user.plan_expired) return '专业版已过期';
    return state.user.plan_label || '免费版';
  }

  function promptUpgrade(feature, message) {
    const label = featureLabel(feature);
    const text = message || ('“' + label + '”需要升级到专业版后使用');
    toastError('专业版功能', text);
    openUrl('/upgrade.html' + (feature ? '?feature=' + encodeURIComponent(feature) : ''));
  }

  // ───────────────────────── 功能加锁 ─────────────────────────

  function lockElement(element, feature) {
    if (element.dataset.featureLocked === '1') return;
    element.dataset.featureLocked = '1';
    element.classList.add('feature-locked');
    // 关键：按钮和链接不能设 disabled —— 浏览器不会给 disabled 元素派发 click，
    // 用户点了没有任何反馈。这里保持“可点击但点不动原逻辑”，点击时才提示升级。
    const tag = element.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      if (element.type === 'checkbox' || element.type === 'radio') {
        element.disabled = true;
      } else {
        element.readOnly = true;
      }
    } else if (tag === 'SELECT') {
      element.disabled = true;
    }
    element.setAttribute('title', '需要专业版：' + featureLabel(feature));
    element.setAttribute('aria-disabled', 'true');
    element.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      promptUpgrade(feature);
    }, true);
  }

  function lockRegion(region, feature) {
    if (region.dataset.featureLocked === '1') return;
    region.dataset.featureLocked = '1';
    region.classList.add('feature-lockable');
    const overlay = document.createElement('div');
    overlay.className = 'feature-lock-overlay';
    overlay.innerHTML = '<span class="feature-lock-chip"><i class="bi bi-lock-fill"></i> ' +
      '专业版功能 · 点此升级解锁</span>';
    overlay.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      promptUpgrade(feature);
    }, true);
    region.appendChild(overlay);
  }

  function applyGates(root) {
    const scope = root || document;
    if (!state.auth_enabled) return;
    scope.querySelectorAll('[data-feature]').forEach((element) => {
      const feature = element.dataset.feature;
      if (can(feature)) {
        element.dataset.featureUnlocked = '1';
        return;
      }
      lockElement(element, feature);
    });
    scope.querySelectorAll('[data-feature-lock]').forEach((region) => {
      const feature = region.dataset.featureLock;
      if (can(feature)) return;
      lockRegion(region, feature);
    });
    // 「看不见」类入口（data-hide-without）统一由 common.js 的导航自己判断：
    // 导航在所有页面都存在，但很多页面并没装 auth.js，只有放在导航里才处处生效。
    // 这里喊它重算一次，登录状态变化（如刚兑换完激活码）后也会跟着更新。
    if (global.LT && LT.Nav && typeof LT.Nav.applyVisibilityRules === 'function') {
      LT.Nav.applyVisibilityRules();
    }
  }

  let gateTimer = null;
  function watchDom() {
    if (typeof MutationObserver === 'undefined') return;
    const observer = new MutationObserver(() => {
      clearTimeout(gateTimer);
      gateTimer = setTimeout(() => applyGates(), 60);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // ───────────────────────── 界面 ─────────────────────────

  function planClass() {
    if (!state.user) return 'auth-plan is-free';
    if (state.user.is_admin) return 'auth-plan is-admin';
    if (state.user.plan_expired) return 'auth-plan is-expired';
    if (state.user.is_paid) return 'auth-plan';
    return 'auth-plan is-free';
  }

  function renderBar(target) {
    const host = typeof target === 'string' ? $(target) : target;
    if (!host) return;
    let bar = $('#authBar', host.parentElement || document);
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'authBar';
      bar.className = 'auth-bar';
      host.appendChild(bar);
    }
    if (!state.auth_enabled) {
      bar.innerHTML = '';
      return;
    }
    const user = state.user;
    if (!user) {
      bar.innerHTML = '<a class="btn btn-primary btn-sm" href="/login.html"><i class="bi bi-box-arrow-in-right"></i> 登录</a>';
      return;
    }
    const days = user.plan_days_left;
    const expiry = user.plan_expires_at
      ? '<span class="hint" style="font-size:11.5px">' +
        (user.plan_expired ? '已过期 ' : '剩余 ' + (days === null ? '' : days + ' 天 · ')) +
        (user.plan_expires_at || '').slice(0, 10) + '</span>'
      : '';
    bar.innerHTML =
      '<span class="auth-user"><i class="bi bi-person-circle"></i>' +
      escapeHtml(user.display_name || user.username) +
      '<span class="' + planClass() + '">' + escapeHtml(planLabel()) + '</span>' + expiry + '</span>' +
      (user.is_paid ? '' :
        '<a class="btn btn-primary btn-sm" href="/upgrade.html"><i class="bi bi-gem"></i> 升级会员</a>') +
      (user.is_admin ? '<a class="btn btn-outline btn-sm" href="/admin_users.html"><i class="bi bi-people"></i> 用户管理</a>' : '') +
      '<button class="btn btn-outline btn-sm" type="button" data-auth-action="password"><i class="bi bi-key"></i> 修改密码</button>' +
      '<button class="btn btn-outline btn-sm" type="button" data-auth-action="logout"><i class="bi bi-box-arrow-right"></i> 退出</button>';
  }

  function renderBanner(container) {
    const host = typeof container === 'string' ? $(container) : container;
    if (!host || !state.auth_enabled || !state.authenticated) return;
    let banner = document.getElementById('authBanner');
    if (banner) banner.remove();
    const user = state.user || {};
    let html = '';
    if (state.must_change_password) {
      html = '<div class="auth-banner is-warn" id="authBanner">' +
        '<div><b><i class="bi bi-shield-exclamation"></i> 你仍在使用初始管理员密码</b>' +
        '<p class="hint">为了账号安全，请立即修改密码；初始密码是公开的默认值。</p></div>' +
        '<div class="auth-banner-actions">' +
        '<button class="btn btn-primary btn-sm" type="button" data-auth-action="password">立即修改密码</button>' +
        '</div></div>';
    } else if (!user.is_paid) {
      const plan = state.plans.pro || {};
      html = '<div class="auth-banner" id="authBanner">' +
        '<div><b><i class="bi bi-gem"></i> 当前为免费版：只能查看与试听</b>' +
        '<p class="hint">可以查看工作台、商品库和做模拟评论测试；连接数字人、开播、自动话术、录制、' +
        '字幕回应、商品与佣金管理需要升级到专业版（' + escapeHtml(plan.price || '开通后解锁') + '）。</p></div>' +
        '<div class="auth-banner-actions">' +
        '<a class="btn btn-primary btn-sm" href="/upgrade.html"><i class="bi bi-gem"></i> 升级会员</a>' +
        '<a class="btn btn-outline btn-sm" href="/affiliate_tutorial.html" target="_blank" rel="noopener">先看教程</a>' +
        '</div></div>';
    }
    if (html) host.insertAdjacentHTML('afterbegin', html);
  }

  function escapeHtml(text) {
    return String(text === undefined || text === null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // 密码规则必须和 server/auth_manager.py 的 password_problem 保持一致：
  // 至少 8 位，且不能是纯数字或纯字母（纯数字会被服务端拒绝）。
  const MIN_PASSWORD_LENGTH = 8;
  const MAX_PASSWORD_LENGTH = 128;

  function localPasswordProblem(password) {
    if (!password || password.length < MIN_PASSWORD_LENGTH) {
      return '密码至少需要 ' + MIN_PASSWORD_LENGTH + ' 位';
    }
    if (password.length > MAX_PASSWORD_LENGTH) {
      return '密码过长（最多 ' + MAX_PASSWORD_LENGTH + ' 位）';
    }
    if (/^\d+$/.test(password) || /^[A-Za-z]+$/.test(password)) {
      return '密码需要同时包含字母和数字（纯数字、纯字母都会被拒绝），例如 ailive2026';
    }
    return null;
  }

  function openPasswordModal() {
    if ($('#authPasswordModal')) return;
    const backdrop = document.createElement('div');
    backdrop.className = 'auth-modal-backdrop';
    backdrop.id = 'authPasswordModal';
    backdrop.innerHTML = `
      <div class="auth-modal" role="dialog" aria-modal="true" aria-label="修改密码">
        <div class="auth-modal-head"><span><i class="bi bi-key"></i> 修改密码</span>
          <button class="btn btn-outline btn-sm" type="button" data-modal-close>&times;</button></div>
        <div class="auth-modal-body">
          <div class="form-group"><label class="form-label" for="authOldPw">当前密码</label>
            <input class="form-control" type="password" id="authOldPw" autocomplete="current-password"></div>
          <div class="form-group"><label class="form-label" for="authNewPw">新密码（至少 8 位，含字母和数字）</label>
            <input class="form-control" type="password" id="authNewPw" autocomplete="new-password"></div>
          <div class="form-group"><label class="form-label" for="authNewPw2">确认新密码</label>
            <input class="form-control" type="password" id="authNewPw2" autocomplete="new-password"></div>
          <div id="authPwMessage" class="hint"></div>
        </div>
        <div class="auth-modal-foot">
          <button class="btn btn-outline" type="button" data-modal-close>取消</button>
          <button class="btn btn-primary" type="button" id="authPwSubmit">保存新密码</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);

    backdrop.querySelectorAll('[data-modal-close]').forEach((button) => {
      button.addEventListener('click', () => backdrop.remove());
    });
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) backdrop.remove();
    });
    $('#authPwSubmit', backdrop).addEventListener('click', async () => {
      const oldPassword = $('#authOldPw', backdrop).value;
      const newPassword = $('#authNewPw', backdrop).value;
      const confirmPassword = $('#authNewPw2', backdrop).value;
      const message = $('#authPwMessage', backdrop);
      if (newPassword !== confirmPassword) {
        message.textContent = '两次输入的新密码不一致';
        message.classList.add('is-error');
        return;
      }
      // 先在本地按同样规则校验：否则纯数字密码要等服务器返回才知道被拒，
      // 提示又只是一行灰色小字，很容易被当成"改成功了"。
      const localProblem = localPasswordProblem(newPassword);
      if (localProblem) {
        message.textContent = localProblem;
        message.classList.add('is-error');
        $('#authNewPw', backdrop).focus();
        return;
      }
      message.classList.remove('is-error');
      try {
        await changePassword(oldPassword, newPassword);
        backdrop.remove();
        toastSuccess('密码已更新', '其他设备上的登录状态已失效');
      } catch (error) {
        message.textContent = error.message;
        message.classList.add('is-error');
      }
    });
  }

  function bindActions() {
    document.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-auth-action]');
      if (!trigger) return;
      const action = trigger.dataset.authAction;
      if (action === 'logout') {
        event.preventDefault();
        logout();
      } else if (action === 'password') {
        event.preventDefault();
        openPasswordModal();
      }
    });
  }

  // ───────────────────────── 对外动作 ─────────────────────────

  async function logout() {
    try {
      await request('/api/auth/logout', { method: 'POST', silent: true });
    } catch (error) {
      console.warn(error);
    }
    location.href = '/login.html';
  }

  function redeem(code) {
    return request('/api/auth/redeem', { method: 'POST', body: { code: code } });
  }

  function changePassword(oldPassword, newPassword) {
    return request('/api/auth/password', {
      method: 'POST',
      body: { old_password: oldPassword, new_password: newPassword },
      silent: true,
    });
  }

  // 初始密码提醒：顶部横幅一直保留，但弹窗每个浏览器会话最多自动弹一次，
  // 否则每次页面加载都会盖住界面、挡住后台操作。
  function takePasswordPromptSlot() {
    try {
      if (sessionStorage.getItem('authPwPrompted') === '1') return false;
      sessionStorage.setItem('authPwPrompted', '1');
    } catch (error) {
      return true;
    }
    return true;
  }

  function mount(options) {
    const opts = options || {};
    injectStyle();
    const ready = load(true).then((current) => {
      if (current.auth_enabled && !current.authenticated && opts.requireLogin !== false) {
        const next = encodeURIComponent(location.pathname + location.search);
        location.href = '/login.html?next=' + next;
        return current;
      }
      renderBar(opts.into || '.page-heading .actions');
      if (opts.banner !== false) {
        renderBanner(opts.bannerInto || 'main .container, main');
      }
      applyGates();
      watchDom();
      if (current.must_change_password && opts.promptPasswordChange !== false
          && takePasswordPromptSlot()) {
        openPasswordModal();
      }
      return current;
    });
    return ready;
  }

  bindActions();

  global.Auth = {
    state: state,
    load: load,
    mount: mount,
    can: can,
    isAdmin: isAdmin,
    isPaid: isPaid,
    planLabel: planLabel,
    user: function () { return state.user; },
    featureLabel: featureLabel,
    applyGates: applyGates,
    promptUpgrade: promptUpgrade,
    openPasswordModal: openPasswordModal,
    request: request,
    logout: logout,
    redeem: redeem,
    changePassword: changePassword,
    onChange: function (callback) { listeners.push(callback); },
    ready: null,
  };
})(window);