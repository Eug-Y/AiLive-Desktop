/* ═══════════════════════════════════════════════
   LiveTalking 共享工具库 v2
   通用功能：Toast、API、导航、状态管理
   依赖：common.css（视觉样式）
   ═══════════════════════════════════════════════ */

(function (global) {
  'use strict';

  // ═══════════════════════════════════════════
  // Toast 通知系统
  // ═══════════════════════════════════════════
  const Toast = {
    _container: null,
    _icons: {
      success: 'bi-check-circle-fill',
      error: 'bi-x-circle-fill',
      warning: 'bi-exclamation-triangle-fill',
      info: 'bi-info-circle-fill',
    },

    _ensureContainer() {
      if (!this._container) {
        this._container = document.createElement('div');
        this._container.className = 'toast-container';
        this._container.id = 'lt-toast-container';
        document.body.appendChild(this._container);
      }
      return this._container;
    },

    show(title, message, type = 'info', duration = 4000) {
      this._ensureContainer();

      const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️',
      };

      const toast = document.createElement('div');
      toast.className = `toast ${type}`;
      toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <div class="toast-body">
          <div class="toast-title">${this._escape(title)}</div>
          ${message ? `<div class="toast-message">${this._escape(message)}</div>` : ''}
        </div>
        <button class="toast-close" aria-label="关闭">&times;</button>
      `;

      toast.querySelector('.toast-close').onclick = () => this._remove(toast);

      this._container.appendChild(toast);

      if (duration > 0) {
        setTimeout(() => this._remove(toast), duration);
      }

      return toast;
    },

    success(title, message, duration) {
      return this.show(title, message, 'success', duration);
    },
    error(title, message, duration) {
      return this.show(title, message, 'error', duration || 6000);
    },
    warning(title, message, duration) {
      return this.show(title, message, 'warning', duration);
    },
    info(title, message, duration) {
      return this.show(title, message, 'info', duration);
    },

    _remove(toast) {
      if (!toast || toast.classList.contains('removing')) return;
      toast.classList.add('removing');
      setTimeout(() => {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 260);
    },

    _escape(str) {
      const div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    },
  };

  // ═══════════════════════════════════════════
  // API 助手
  // ═══════════════════════════════════════════
  const Api = {
    baseURL: '',

    async request(url, options = {}) {
      const {
        method = 'GET',
        body,
        headers = {},
        timeout = 15000,
        showError = true,
      } = options;

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);

      const fetchOptions = {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...headers,
        },
        signal: controller.signal,
      };

      if (body && method !== 'GET') {
        fetchOptions.body = JSON.stringify(body);
      }

      try {
        const fullURL = url.startsWith('http') ? url : this.baseURL + url;
        const response = await fetch(fullURL, fetchOptions);
        clearTimeout(timer);

        const data = await response.json();

        if (data.code && data.code !== 0) {
          const error = new Error(data.msg || data.message || '服务器返回错误');
          error.code = data.code;
          error.data = data;
          throw error;
        }

        return data;
      } catch (err) {
        clearTimeout(timer);

        if (err.name === 'AbortError') {
          const timeoutErr = new Error('请求超时，请检查网络连接');
          if (showError) Toast.error('请求超时', '请检查服务器是否正常运行');
          throw timeoutErr;
        }

        if (err.message === 'Failed to fetch') {
          const netErr = new Error('无法连接到服务器');
          if (showError) Toast.error('网络错误', '请检查服务器是否正常运行');
          throw netErr;
        }

        if (showError && !err._silent) {
          Toast.error('操作失败', err.message);
        }

        throw err;
      }
    },

    get(url, options) {
      return this.request(url, { ...options, method: 'GET' });
    },
    post(url, body, options) {
      return this.request(url, { ...options, method: 'POST', body });
    },
    put(url, body, options) {
      return this.request(url, { ...options, method: 'PUT', body });
    },
    delete(url, options) {
      return this.request(url, { ...options, method: 'DELETE' });
    },

    /** 上传文件 */
    async upload(url, file, extraFields = {}) {
      const formData = new FormData();
      formData.append('file', file);
      Object.entries(extraFields).forEach(([k, v]) => formData.append(k, v));

      try {
        const response = await fetch(url, { method: 'POST', body: formData });
        const data = await response.json();
        if (data.code && data.code !== 0) throw new Error(data.msg);
        return data;
      } catch (err) {
        if (!err._silent) Toast.error('上传失败', err.message);
        throw err;
      }
    },
  };

  // ═══════════════════════════════════════════
  // 导航组件 (自动注入)
  // ═══════════════════════════════════════════
  const Nav = {
    /** 当前页面标识 (需各页面在调用前设置) */
    current: '',

    /** 常驻一级入口：最高频的三个页面直接可见 */
    _primary: [
      { id: 'live_console', href: '/live_console.html', icon: 'bi-broadcast-pin', label: '直播工作台' },
      { id: 'index', href: '/index.html', icon: 'bi-camera-video', label: '数字人驱动' },
      { id: 'dashboard', href: '/dashboard.html', icon: 'bi-grid-1x2', label: '交互面板' },
    ],

    /** 二级入口按用途分组，避免 10 项平铺挤在一条导航里 */
    _groups: [
      {
        id: 'content',
        label: '内容配置',
        icon: 'bi-sliders',
        items: [
          { id: 'avatar', href: '/avatar.html', icon: 'bi-person-bounding-box', label: '数字人形象', desc: 'Avatar 生成与管理' },
          { id: 'llm', href: '/llm.html', icon: 'bi-cpu', label: '大模型', desc: '对话模型与接口配置' },
          { id: 'tts', href: '/tts/index.html', icon: 'bi-soundwave', label: '语音合成', desc: '音色与 TTS 引擎' },
          { id: 'subtitle', href: '/subtitle.html', icon: 'bi-badge-cc', label: '字幕应答', desc: '字幕识别与自动回复' },
          { id: 'virtualcam', href: '/virtualcam.html', icon: 'bi-camera-reels', label: '虚拟摄像头', desc: '输出到直播软件' },
        ],
      },
      {
        id: 'ops',
        label: '运营管理',
        icon: 'bi-gear',
        items: [
          { id: 'accounts', href: '/accounts.html', icon: 'bi-people', label: '账号管理', desc: '直播账号与商品库' },
          { id: 'console', href: '/console.html', icon: 'bi-terminal', label: '控制台', desc: '一键开播与运行日志' },
          { id: 'admin', href: '/admin.html', icon: 'bi-speedometer2', label: '系统管理', desc: '全局配置与活跃会话' },
          { id: 'affiliate', href: '/affiliate_tutorial.html', icon: 'bi-book', label: '带货教程', desc: 'AI 带货直播操作指引' },
        ],
      },
    ],

    /** 单列入口。hideWithout：没有该功能的用户连入口都不显示（由 auth.js 统一处理） */
    _extra: [
      { id: 'competition', href: '/competition.html', icon: 'bi-trophy', label: '参赛项目',
        hideWithout: 'competition.view' },
    ],

    /** 扁平化的全部条目（供外部查询当前页归属） */
    get _items() {
      return [
        ...this._primary,
        ...this._groups.flatMap((group) => group.items),
        ...this._extra,
      ];
    },

    inject(targetSelector) {
      const target =
        typeof targetSelector === 'string'
          ? document.querySelector(targetSelector)
          : targetSelector;

      if (!target) return;

      const link = (item) => `
          <a href="${item.href}" class="lt-nav-link ${this.current === item.id ? 'active' : ''}"
             title="${item.label}"${item.hideWithout ? ` data-hide-without="${item.hideWithout}"` : ''}>
            <i class="bi ${item.icon}"></i> <span class="nav-label">${item.label}</span>
          </a>`;

      const primaryLinks = this._primary.map(link).join('');
      const extraLinks = this._extra.map(link).join('');

      const groups = this._groups
        .map((group) => {
          const containsCurrent = group.items.some((item) => item.id === this.current);
          const menuItems = group.items
            .map(
              (item) => `
              <a href="${item.href}" class="lt-nav-menu-link ${this.current === item.id ? 'active' : ''}"${item.hideWithout ? ` data-hide-without="${item.hideWithout}"` : ''}>
                <i class="bi ${item.icon}"></i>
                <span class="menu-text">
                  <span class="t">${item.label}</span>
                  ${item.desc ? `<span class="d">${item.desc}</span>` : ''}
                </span>
              </a>`
            )
            .join('');
          return `
          <div class="lt-nav-group ${containsCurrent ? 'active open-mark' : ''}" data-group="${group.id}">
            <button type="button" class="lt-nav-link" aria-haspopup="true" aria-expanded="false">
              <i class="bi ${group.icon}"></i>
              <span class="nav-label">${group.label}</span>
              <i class="bi bi-chevron-down caret"></i>
            </button>
            <div class="lt-nav-menu">${menuItems}</div>
          </div>`;
        })
        .join('');

      const links = primaryLinks + groups + extraLinks;

      target.innerHTML = `
        <div class="lt-nav">
          <div class="lt-nav-inner">
            <a href="/live_console.html" class="lt-nav-brand" title="返回直播工作台">
              <i class="bi bi-camera-video-fill"></i> LiveTalking
              <span class="brand-sub">AI 数字人直播</span>
            </a>
            <button class="lt-nav-toggle" id="lt-nav-toggle" aria-label="打开导航菜单" aria-expanded="false">
              <i class="bi bi-list"></i>
            </button>
            <div class="lt-nav-links" id="lt-nav-links">${links}</div>
            <div class="lt-nav-right">
              <select class="lt-account-select" id="lt-account-switcher" aria-label="当前直播账号" title="切换直播账号">
                <option value="">账号加载中…</option>
              </select>
              <div class="conn-quality" id="lt-conn-quality" title="连接质量" style="display:none;">
                <div class="conn-quality-bar"></div><div class="conn-quality-bar"></div>
                <div class="conn-quality-bar"></div><div class="conn-quality-bar"></div>
              </div>
              <span class="lt-nav-status offline" id="lt-nav-status" title="WebRTC 连接状态">
                <span class="status-dot off"></span>
                <span class="lt-nav-status-text">未连接</span>
                <span class="lt-nav-sid" id="lt-nav-sid" hidden></span>
              </span>
              <button class="lt-nav-theme-btn" id="lt-theme-toggle" title="切换主题" aria-label="切换深色/浅色主题">
                <i class="bi bi-moon-fill"></i>
              </button>
            </div>
          </div>
        </div>
      `;

      // 移动端折叠
      const toggle = document.getElementById('lt-nav-toggle');
      const menu = document.getElementById('lt-nav-links');
      if (toggle && menu) {
        toggle.onclick = () => {
          const open = menu.classList.toggle('open');
          toggle.setAttribute('aria-expanded', String(open));
        };
        menu.querySelectorAll('a').forEach((a) => {
          a.addEventListener('click', () => {
            menu.classList.remove('open');
            toggle.setAttribute('aria-expanded', 'false');
          });
        });
      }

      // 分组下拉：点击展开、点击外部或 Esc 收起
      const groupEls = Array.from(target.querySelectorAll('.lt-nav-group'));

      // 菜单是 position:fixed（原因见 common.css 中 .lt-nav-menu 的注释），
      // 因此坐标必须由 JS 算：对齐按钮左边缘，贴近视口右边界时改为右对齐。
      const placeMenu = (el) => {
        const btn = el.querySelector('.lt-nav-link');
        const menu = el.querySelector('.lt-nav-menu');
        if (!btn || !menu) return;
        const rect = btn.getBoundingClientRect();
        const width = menu.offsetWidth || 232;
        let left = rect.left;
        if (left + width > window.innerWidth - 8) {
          left = Math.max(8, window.innerWidth - width - 8);
        }
        menu.style.left = `${Math.round(left)}px`;
        menu.style.top = `${Math.round(rect.bottom + 8)}px`;
      };

      const closeGroups = (except) => {
        groupEls.forEach((el) => {
          if (el !== except) {
            el.classList.remove('open');
            const btn = el.querySelector('.lt-nav-link');
            if (btn) btn.setAttribute('aria-expanded', 'false');
          }
        });
      };

      groupEls.forEach((el) => {
        const btn = el.querySelector('.lt-nav-link');
        if (!btn) return;
        btn.onclick = (event) => {
          event.preventDefault();
          event.stopPropagation();
          const willOpen = !el.classList.contains('open');
          closeGroups(el);
          el.classList.toggle('open', willOpen);
          btn.setAttribute('aria-expanded', String(willOpen));
          if (willOpen) placeMenu(el);
        };
        // 鼠标移开后延迟收起，避免误触
        el.addEventListener('mouseleave', () => {
          setTimeout(() => {
            if (el.classList.contains('open') && !el.matches(':hover')) {
              el.classList.remove('open');
              const b = el.querySelector('.lt-nav-link');
              if (b) b.setAttribute('aria-expanded', 'false');
            }
          }, 260);
        });
      });

      // 导航栏是 sticky，页面滚动时按钮会移动；fixed 菜单必须跟着走，
      // 否则菜单会停在原地与按钮脱节。
      if (!Nav._placeBound) {
        Nav._placeBound = true;
        const follow = () => {
          document.querySelectorAll('.lt-nav-group.open').forEach(placeMenu);
        };
        window.addEventListener('scroll', follow, { passive: true });
        window.addEventListener('resize', follow);
      }

      if (!Nav._outsideBound) {
        Nav._outsideBound = true;
        document.addEventListener('click', (event) => {
          if (!event.target.closest('.lt-nav-group')) closeGroups(null);
        });
        document.addEventListener('keydown', (event) => {
          if (event.key === 'Escape') closeGroups(null);
        });
      }

      // 主题切换按钮
      const themeBtn = document.getElementById('lt-theme-toggle');
      if (themeBtn) {
        themeBtn.onclick = () => Theme.toggle();
        Theme._updateButton(Theme.isDark());
      }

      const accountSwitcher = document.getElementById('lt-account-switcher');
      if (accountSwitcher) {
        accountSwitcher.onchange = async () => {
          const accountId = accountSwitcher.value;
          if (!accountId) return;
          accountSwitcher.disabled = true;
          try {
            await Api.post('/api/accounts/switch', { account_id: accountId }, { showError: false });
            Toast.success('账号已切换', '正在刷新当前工作台');
            setTimeout(() => window.location.reload(), 250);
          } catch (error) {
            Toast.error('账号切换失败', error.message);
            accountSwitcher.disabled = false;
            this.loadAccounts();
          }
        };
        this.loadAccounts();
      }

      this.applyVisibilityRules();
    },

    /**
     * 「只给管理员看」的入口（带 data-hide-without 的元素）：先默认藏起来，确认有权限再显示。
     * 导航是本文件生成的、而鉴权状态在 auth.js，所以这里自己取一次 /api/auth/me ——
     * 页面上装了 auth.js 就直接读它的状态（不重复请求），没装就请求一次并缓存。
     * 默认藏、确认后才显示，是为了普通用户连"一闪而过"都看不到。
     */
    applyVisibilityRules() {
      const targets = Array.from(document.querySelectorAll('[data-hide-without]'));
      if (!targets.length) return Promise.resolve();

      targets.forEach((element) => {
        if (element.dataset.hideApplied !== '1') {
          element.style.display = 'none';
          element.dataset.hideApplied = '1';
        }
      });

      const reveal = (element) => {
        if (element.dataset.hideApplied === '1') {
          element.style.display = '';
          delete element.dataset.hideApplied;
        }
      };
      const apply = (authEnabled, entitlements) => {
        if (!authEnabled) {
          targets.forEach(reveal);   // 关了登录（AILIVE_AUTH=off）：不做任何隐藏
          return;
        }
        targets.forEach((element) => {
          if (entitlements[element.dataset.hideWithout] === true) reveal(element);
        });
      };

      if (window.Auth && window.Auth.state) {
        // auth.js 在场：等它加载完再算（Auth.mount 之后会再调一次本方法）
        if (!window.Auth.state.loaded) return Promise.resolve();
        apply(window.Auth.state.auth_enabled, window.Auth.state.entitlements || {});
        return Promise.resolve();
      }

      if (!Nav._visibilityRequest) {
        Nav._visibilityRequest = fetch('/api/auth/me', { credentials: 'same-origin' })
          .then((response) => (response.ok ? response.json() : null))
          .then((payload) => {
            const data = (payload && payload.data) || {};
            apply(data.auth_enabled !== false, data.entitlements || {});
          })
          .catch(() => {});   // 取不到状态就保持隐藏，刷新即恢复
      }
      return Nav._visibilityRequest;
    },

    async loadAccounts() {
      const select = document.getElementById('lt-account-switcher');
      if (!select) return;
      try {
        const response = await Api.get('/api/accounts', { showError: false });
        const data = response.data || {};
        const accounts = Array.isArray(data.accounts) ? data.accounts : [];
        select.innerHTML = accounts.length
          ? accounts.map((account) =>
              `<option value="${account.id}" ${account.is_active ? 'selected' : ''}>${Utils.escape(account.name)} · ${Utils.escape(account.platform_label || account.platform)}</option>`
            ).join('')
          : '<option value="">暂无账号</option>';
        select.title = data.active_account
          ? `当前账号：${data.active_account.name}`
          : '当前直播账号';
      } catch {
        select.innerHTML = '<option value="">账号不可用</option>';
      }
    },

    /** 更新导航栏连接状态 */
    updateStatus(connected) {
      const el = document.getElementById('lt-nav-status');
      if (!el) return;
      el.className = connected ? 'lt-nav-status online' : 'lt-nav-status offline';
      const dot = el.querySelector('.status-dot');
      if (dot) dot.className = connected ? 'status-dot on' : 'status-dot off';
      const text = el.querySelector('.lt-nav-status-text');
      if (text) text.textContent = connected ? '已连接' : '未连接';
      if (!connected) this.updateSessionId(null);
    },

    /** 更新会话 ID 显示（独立节点，避免覆盖连接状态） */
    updateSessionId(sid) {
      const el = document.getElementById('lt-nav-sid');
      if (!el) return;
      if (!sid) {
        el.hidden = true;
        el.textContent = '';
        return;
      }
      el.hidden = false;
      el.textContent = `会话 ${sid}`;
      el.title = `会话 ID：${sid}`;
    },
  };

  // ═══════════════════════════════════════════
  // 加载状态管理
  // ═══════════════════════════════════════════
  const Loading = {
    /** 在元素上显示加载 */
    button(el, loading = true) {
      const btn = typeof el === 'string' ? document.querySelector(el) : el;
      if (!btn) return;
      if (loading) {
        btn.classList.add('loading');
        btn.disabled = true;
      } else {
        btn.classList.remove('loading');
        btn.disabled = false;
      }
    },

    /** 在容器中显示骨架屏 */
    skeleton(container, count = 3) {
      const el =
        typeof container === 'string'
          ? document.querySelector(container)
          : container;
      if (!el) return;
      el.innerHTML = Array.from(
        { length: count },
        () => '<div class="skeleton skeleton-text"></div>'
      ).join('');
    },

    /** 在容器中显示旋转加载 */
    spinner(container, size = '') {
      const el =
        typeof container === 'string'
          ? document.querySelector(container)
          : container;
      if (!el) return;
      el.innerHTML = `<div class="text-center py-3"><div class="spinner ${size}"></div></div>`;
    },
  };

  // ═══════════════════════════════════════════
  // 工具函数
  // ═══════════════════════════════════════════
  const Utils = {
    /** 获取URL参数 */
    getParam(name) {
      const params = new URLSearchParams(window.location.search);
      return params.get(name);
    },

    /** 格式化时间 */
    formatTime(ts) {
      if (!ts) return '-';
      const d = new Date(ts * 1000);
      return d.toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    },

    /** 格式化时长 */
    formatDuration(seconds) {
      if (!seconds) return '0m';
      const s = Math.floor(seconds);
      const m = Math.floor(s / 60);
      const h = Math.floor(m / 60);
      if (h > 0) return `${h}h${m % 60}m`;
      if (m > 0) return `${m}m${s % 60}s`;
      return `${s}s`;
    },

    /** 防抖 */
    debounce(fn, delay = 300) {
      let timer;
      return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
      };
    },

    /** 转义HTML */
    escape(str) {
      const div = document.createElement('div');
      div.textContent = str;
      return div.innerHTML;
    },

    /** 多行文本转数组 */
    lines(textareaId) {
      const el = document.getElementById(textareaId);
      if (!el) return [];
      return el.value
        .split(/\r?\n/)
        .map((x) => x.trim())
        .filter(Boolean);
    },

    /** 简单的模板渲染 */
    template(str, data) {
      return str.replace(/\{(\w+)\}/g, (_, key) =>
        data[key] !== undefined ? data[key] : `{${key}}`
      );
    },

    /** 复制到剪贴板 */
    async copyToClipboard(text) {
      try {
        await navigator.clipboard.writeText(text);
        Toast.success('已复制', '内容已复制到剪贴板');
        return true;
      } catch {
        // fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        Toast.success('已复制', '内容已复制到剪贴板');
        return true;
      }
    },
  };

  // ═══════════════════════════════════════════
  // 本地存储封装
  // ═══════════════════════════════════════════
  const Store = {
    _prefix: 'lt_',

    get(key, defaultValue) {
      try {
        const raw = localStorage.getItem(this._prefix + key);
        if (raw === null) return defaultValue;
        return JSON.parse(raw);
      } catch {
        return defaultValue;
      }
    },

    set(key, value) {
      try {
        localStorage.setItem(this._prefix + key, JSON.stringify(value));
        return true;
      } catch {
        return false;
      }
    },

    remove(key) {
      localStorage.removeItem(this._prefix + key);
    },
  };

  // ═══════════════════════════════════════════
  // 主题管理器
  // ═══════════════════════════════════════════
  const Theme = {
    _key: 'theme',

    /** 获取当前主题 ('light' | 'dark' | 'auto') */
    get() {
      return Store.get(this._key, 'auto');
    },

    /** 设置主题 */
    set(mode) {
      Store.set(this._key, mode);
      this.apply();
    },

    /** 切换 light <-> dark */
    toggle() {
      const current = this.isDark() ? 'light' : 'dark';
      this.set(current);
      return current;
    },

    /** 应用主题到 DOM */
    apply() {
      const mode = this.get();
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      const isDark = mode === 'dark' || (mode === 'auto' && prefersDark);
      document.body.classList.toggle('dark', isDark);
      this._updateButton(isDark);
    },

    /** 当前是否为暗色 */
    isDark() {
      return document.body.classList.contains('dark');
    },

    /** 初始化：应用主题并监听系统变化 */
    init() {
      this.apply();
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (this.get() === 'auto') this.apply();
      });
    },

    _updateButton(isDark) {
      const btn = document.getElementById('lt-theme-toggle');
      if (btn) {
        btn.title = isDark ? '切换到亮色模式' : '切换到暗色模式';
        btn.innerHTML = isDark
          ? '<i class="bi bi-sun-fill"></i>'
          : '<i class="bi bi-moon-fill"></i>';
      }
    },
  };

  // ═══════════════════════════════════════════
  // 跨标签页状态同步 (BroadcastChannel)
  // ═══════════════════════════════════════════
  const Sync = {
    _channel: null,
    _handlers: {},

    init() {
      try {
        this._channel = new BroadcastChannel('livetalking_sync');
        this._channel.onmessage = (e) => {
          const { type, data } = e.data;
          const handlers = this._handlers[type] || [];
          handlers.forEach((fn) => fn(data));
        };
      } catch {
        // BroadcastChannel 不可用时静默忽略
      }
    },

    send(type, data) {
      if (this._channel) {
        this._channel.postMessage({ type, data });
      }
    },

    on(type, handler) {
      if (!this._handlers[type]) this._handlers[type] = [];
      this._handlers[type].push(handler);
    },

    /** 广播连接状态到其他标签页 */
    broadcastStatus(connected, sessionId) {
      this.send('connection', { connected, sessionId, time: Date.now() });
    },
  };

  // ═══════════════════════════════════════════
  // 自动重连管理器
  // ═══════════════════════════════════════════
  const Reconnect = {
    _maxAttempts: 5,
    _baseDelay: 1000,
    _maxDelay: 30000,
    _attempt: 0,
    _onReconnect: null,
    _timer: null,
    _active: false,

    /** 开始自动重连 */
    start(onReconnect, options = {}) {
      if (this._active) return;
      this._active = true;
      this._attempt = 0;
      this._onReconnect = onReconnect;
      this._maxAttempts = options.maxAttempts || 5;
      this._baseDelay = options.baseDelay || 1000;
      this._maxDelay = options.maxDelay || 30000;
      this._showBanner();
      this._tryReconnect();
    },

    /** 停止自动重连 */
    stop() {
      this._active = false;
      this._attempt = 0;
      if (this._timer) clearTimeout(this._timer);
      this._hideBanner();
    },

    /** 重置（连接成功后调用） */
    reset() {
      this._active = false;
      this._attempt = 0;
      if (this._timer) clearTimeout(this._timer);
      this._hideBanner();
    },

    _tryReconnect() {
      if (!this._active) return;
      this._attempt++;
      if (this._attempt > this._maxAttempts) {
        this._hideBanner();
        Toast.error('重连失败', `已尝试 ${this._maxAttempts} 次，请手动重新连接`);
        this._active = false;
        return;
      }

      this._updateBanner(`正在重连 (${this._attempt}/${this._maxAttempts})...`);

      if (this._onReconnect) {
        Promise.resolve(this._onReconnect())
          .then((success) => {
            if (success) {
              this.reset();
              Toast.success('已重新连接');
            } else {
              this._scheduleNext();
            }
          })
          .catch(() => this._scheduleNext());
      }
    },

    _scheduleNext() {
      const delay = Math.min(this._baseDelay * Math.pow(2, this._attempt - 1), this._maxDelay);
      this._timer = setTimeout(() => this._tryReconnect(), delay);
    },

    _showBanner() {
      if (document.getElementById('lt-reconnect-banner')) return;
      const banner = document.createElement('div');
      banner.id = 'lt-reconnect-banner';
      banner.className = 'reconnect-banner';
      banner.innerHTML = '<div class="spinner spinner-sm"></div> <span>连接断开，正在重连...</span>';
      document.body.prepend(banner);
    },

    _updateBanner(text) {
      const banner = document.getElementById('lt-reconnect-banner');
      if (banner) {
        const span = banner.querySelector('span');
        if (span) span.textContent = text;
      }
    },

    _hideBanner() {
      const banner = document.getElementById('lt-reconnect-banner');
      if (banner) banner.remove();
    },
  };

  // ═══════════════════════════════════════════
  // 全局键盘快捷键
  // ═══════════════════════════════════════════
  const Keys = {
    _bindings: {},

    /** 绑定快捷键 */
    bind(combo, handler, description = '') {
      const key = this._normalize(combo);
      if (!this._bindings[key]) this._bindings[key] = [];
      this._bindings[key].push({ handler, description, combo });
    },

    /** 获取所有绑定的描述 */
    list() {
      const result = [];
      Object.values(this._bindings).forEach((arr) => {
        arr.forEach((b) => result.push({ combo: b.combo, description: b.description }));
      });
      return result;
    },

    _normalize(combo) {
      return combo
        .toLowerCase()
        .split('+')
        .map((k) => k.trim())
        .sort()
        .join('+');
    },

    _handle(e) {
      const parts = [];
      if (e.ctrlKey || e.metaKey) parts.push('ctrl');
      if (e.altKey) parts.push('alt');
      if (e.shiftKey) parts.push('shift');
      if (e.key && !['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) {
        parts.push(e.key.toLowerCase());
      }
      const key = parts.sort().join('+');
      const handlers = this._bindings[key];
      if (handlers && handlers.length > 0) {
        e.preventDefault();
        handlers.forEach((h) => h.handler(e));
      }
    },

    init() {
      document.addEventListener('keydown', (e) => this._handle(e));
    },
  };

  // ═══════════════════════════════════════════
  // 说话状态指示器
  // ═══════════════════════════════════════════
  const Speaking = {
    _el: null,
    _bars: [],

    /** 创建音频可视化条 */
    createVisualizer(container, barCount = 8) {
      const el = typeof container === 'string' ? document.querySelector(container) : container;
      if (!el) return;
      el.className = (el.className + ' audio-viz').trim();
      for (let i = 0; i < barCount; i++) {
        const bar = document.createElement('div');
        bar.className = 'audio-viz-bar';
        el.appendChild(bar);
        this._bars.push(bar);
      }
      this._el = el;
    },

    /** 更新可视化 */
    updateVisualizer(intensity = 0) {
      if (!this._el) return;
      const active = intensity > 0.05;
      this._el.classList.toggle('speaking', active);
      this._bars.forEach((bar, i) => {
        const h = active
          ? Math.max(2, Math.sin(i * 0.8 + Date.now() * 0.01) * intensity * 24 + intensity * 6)
          : 2;
        bar.style.height = h + 'px';
      });
    },

    /** 开始可视化动画循环 */
    startVisualizer() {
      if (this._animating) return;
      this._animating = true;
      const loop = () => {
        if (!this._animating) return;
        this.updateVisualizer(0.5 + Math.sin(Date.now() * 0.005) * 0.3);
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    },

    /** 停止可视化动画 */
    stopVisualizer() {
      this._animating = false;
      this.updateVisualizer(0);
    },
  };

  // ═══════════════════════════════════════════
  // 连接质量监控
  // ═══════════════════════════════════════════
  const ConnMonitor = {
    _pc: null, _interval: null, _onUpdate: null,

    watch(pc, onUpdate) {
      this._pc = pc; this._onUpdate = onUpdate;
      this._interval = setInterval(() => this._poll(), 3000);
    },

    stop() {
      if (this._interval) { clearInterval(this._interval); this._interval = null; }
      this._pc = null;
    },

    async _poll() {
      if (!this._pc) return;
      try {
        const stats = await this._pc.getStats();
        let rtt = 0, packetsLost = 0, packetsReceived = 0, bitrate = 0;
        stats.forEach(r => {
          if (r.type === 'candidate-pair' && r.state === 'succeeded') rtt = r.currentRoundTripTime || 0;
          if (r.type === 'inbound-rtp' && r.kind === 'video') {
            packetsLost = r.packetsLost || 0; packetsReceived = r.packetsReceived || 0;
            bitrate = r.bitrateMean || r.estimatedBitrate || 0;
          }
        });
        const lossRate = packetsReceived > 0 ? packetsLost / (packetsLost + packetsReceived) : 0;
        const quality = rtt < 0.1 ? 'good' : rtt < 0.3 ? 'poor' : 'bad';
        const el = document.getElementById('lt-conn-quality');
        if (el) {
          el.style.display = 'inline-flex';
          el.className = 'conn-quality ' + quality;
          el.title = `RTT: ${(rtt*1000).toFixed(0)}ms · 丢包: ${(lossRate*100).toFixed(1)}% · 码率: ${(bitrate/1e6).toFixed(1)}Mbps`;
        }
        if (this._onUpdate) this._onUpdate({ rtt, lossRate, bitrate, quality });
      } catch {}
    },
  };

  // ═══════════════════════════════════════════
  // 浏览器通知
  // ═══════════════════════════════════════════
  const Notify = {
    _permission: 'default',

    async request() {
      if (!('Notification' in window)) return false;
      const result = await Notification.requestPermission();
      this._permission = result;
      return result === 'granted';
    },

    send(title, options = {}) {
      if (!('Notification' in window)) return;
      if (Notification.permission === 'granted') {
        return new Notification(title, {
          icon: '/assets/icon-192.png',
          badge: '/assets/icon-192.png',
          tag: options.tag || 'livetalking',
          body: options.body || '',
          requireInteraction: options.sticky || false,
          ...options,
        });
      }
    },

    connectionChanged(connected) {
      if (connected) this.send('LiveTalking 已连接', { body: '数字人会话已建立', timeout: 3000 });
    },

    speakingStarted(text) {
      this.send('数字人正在说话', { body: text || '正在播报...', timeout: 2000 });
    },
  };

  // ═══════════════════════════════════════════
  // 全局错误边界
  // ═══════════════════════════════════════════
  const ErrorBoundary = {
    _throttled: false,
    init() {
      window.addEventListener('error', (e) => {
        if (e.target && e.target !== window) return;
        console.error('Unhandled error:', e.error);
        if (this._throttled) return;
        this._throttled = true;
        Toast.error('发生未知错误', '请刷新页面后重试', 8000);
        setTimeout(() => { this._throttled = false; }, 10000);
      });
      window.addEventListener('unhandledrejection', (e) => {
        console.error('Unhandled rejection:', e.reason);
        if (e.reason && e.reason._silent) return;
        if (this._throttled) return;
        this._throttled = true;
        Toast.error('操作失败', (e.reason && e.reason.message) || '未知错误', 6000);
        setTimeout(() => { this._throttled = false; }, 10000);
      });
    },
  };

  // ═══════════════════════════════════════════
  // 暴露到全局
  // ═══════════════════════════════════════════
  global.LT = {
    Toast, Api, Nav, Loading, Utils,
    Store, Theme, Sync, Reconnect, Keys, Speaking,
    Notify, ErrorBoundary, ConnMonitor,
  };

  global.LTToast = Toast; global.LTApi = Api; global.LTNav = Nav;
  global.LTLoading = Loading; global.LTUtils = Utils;
  global.LTStore = Store; global.LTTheme = Theme; global.LTSync = Sync;
  global.LTReconnect = Reconnect; global.LTKeys = Keys;
  global.LTSpeaking = Speaking; global.LTNotify = Notify;
  global.LTErrorBoundary = ErrorBoundary; global.LTConnMonitor = ConnMonitor;

  // ── 自动初始化 ────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    Theme.init();
    Sync.init();
    Keys.init();
    ErrorBoundary.init();

    // PWA: 注入manifest链接
    if (!document.querySelector('link[rel="manifest"]')) {
      const manifestLink = document.createElement('link');
      manifestLink.rel = 'manifest';
      manifestLink.href = '/manifest.json';
      document.head.appendChild(manifestLink);
    }

    // PWA: 注册 Service Worker
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
  });
})(window);
