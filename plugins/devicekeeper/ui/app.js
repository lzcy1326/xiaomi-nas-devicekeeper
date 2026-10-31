(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  // The Windows desktop wrapper may expose a /D:/plugin/... path. Keep only
  // the firmware's /plugin/<user>/<plugin>/ segment for API resolution.
  const PLUGIN_PATH_RE = /\/plugin\/[^/?#]+\/[^/?#]+\//;

  function resolveApiRoot() {
    const locationHref = window.location.href;
    const scriptSrc = document.currentScript?.src || '';
    const candidates = [locationHref, document.baseURI, scriptSrc];
    const pluginMatch = candidates
      .map((value) => String(value || '').match(PLUGIN_PATH_RE))
      .find(Boolean);
    const basePath = pluginMatch?.[0] || window.location.pathname.replace(/[^/]*$/, '');
    const originMatch = candidates
      .map((value) => String(value || '').match(/^(https?:\/\/[^/]+)/i))
      .find(Boolean);
    const base = originMatch?.[1] ? `${originMatch[1]}/` : locationHref;
    return new URL(`${basePath}api/v1/`, base);
  }

  const API_ROOT = resolveApiRoot();
  console.info('[devicekeeper] api root', API_ROOT.href);

  const pageNames = {
    overview: '设备管家',
    storage: '存储空间',
    services: '运行服务',
    events: '监测记录',
  };
  const metricConfig = {
    cpu: { key: 'cpu.busy_pct', title: '处理器使用率', unit: '%', input: 'percent' },
    memory: { key: 'memory.used_pct', title: '内存使用率', unit: '%', input: 'percent' },
    temp: { key: 'temperature.cpu_c', title: 'CPU 温度', unit: '°C', input: 'temperature' },
    network: { key: 'network.rx_bytes_per_sec', title: '网络接收速率', unit: 'MiB/s', input: 'rate' },
  };
  const rangeConfig = {
    '1h': { label: '1 小时', seconds: 3600 },
    '24h': { label: '24 小时', seconds: 86400 },
    '7d': { label: '7 天', seconds: 604800 },
  };

  const state = {
    snapshot: null,
    snapshotAge: null,
    history: null,
    events: [],
    page: 'overview',
    metric: 'cpu',
    range: '24h',
    inFlight: false,
    retryDelay: 5000,
    pollTimer: null,
    historySeq: 0,
  };

  const templates = {
    service: $('#service-row-template'),
    disk: $('#disk-row-template'),
    event: $('#event-row-template'),
  };

  function apiUrl(path) {
    return new URL(path, API_ROOT);
  }

  async function fetchJson(path, options = {}) {
    const response = await fetch(apiUrl(path), {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      ...options,
    });
    if (!response.ok) {
      let body = null;
      try { body = await response.json(); } catch (_) { /* non-JSON error */ }
      const error = new Error(body?.message || `HTTP ${response.status}`);
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return response.json();
  }

  function setText(selectorOrNode, value) {
    const node = typeof selectorOrNode === 'string' ? $(selectorOrNode) : selectorOrNode;
    if (node) node.textContent = value;
  }

  function setClass(node, name, on) {
    if (node) node.classList.toggle(name, Boolean(on));
  }

  function toast(message) {
    const node = $('#toast');
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3400);
  }

  function formatNumber(value, digits = 1) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
    return Number(value).toFixed(digits).replace(/\.0$/, digits ? '' : '.0');
  }

  function formatPercent(value, digits = 1) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
    return `${Number(value).toFixed(digits).replace(/\.0$/, '')}%`;
  }

  function formatTemperature(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '--';
    return `${Number(value).toFixed(Number(value) % 1 ? 1 : 0)}°C`;
  }

  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined || !Number.isFinite(Number(bytes))) {
      return { value: '--', unit: '' };
    }
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let value = Math.max(0, Number(bytes));
    let index = 0;
    while (value >= 1000 && index < units.length - 1) {
      value /= 1000;
      index += 1;
    }
    return {
      value: value.toFixed(value >= 100 ? 1 : 2),
      unit: units[index],
    };
  }

  function formatRate(bytesPerSecond) {
    if (bytesPerSecond === null || bytesPerSecond === undefined || !Number.isFinite(Number(bytesPerSecond))) {
      return { value: '--', unit: 'B/s' };
    }
    let value = Math.max(0, Number(bytesPerSecond));
    const units = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return {
      value: Number(value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2)).toString(),
      unit: units[index],
    };
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return '--';
    const total = Math.max(0, Math.floor(Number(seconds)));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days > 0) return `${days} 天 ${hours} 小时`;
    if (hours > 0) return `${hours} 小时 ${minutes} 分钟`;
    return `${minutes} 分钟`;
  }

  function formatTime(value) {
    if (!value) return '--';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--';
    return new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(date);
  }

  function formatShortTime(value) {
    if (!value) return '--';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '--';
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date);
  }

  function go(page) {
    if (!pageNames[page]) return;
    state.page = page;
    $$('.page').forEach((node) => {
      node.hidden = node.id !== `page-${page}`;
    });
    $$('[data-page]').forEach((button) => {
      const active = button.dataset.page === page;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    setText('#page-title', pageNames[page]);
    document.title = `${pageNames[page]} · 设备管家`;
    if (page === 'events') loadEvents();
    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  function setConnection(kind, detail = '') {
    const label = $('#connection-label');
    label.classList.remove('warn', 'error');
    const texts = {
      starting: '正在采集',
      ok: '本地监测',
      partial: '部分数据',
      stale: '数据已过期',
      error: '连接中断',
    };
    label.textContent = texts[kind] || '本地监测';
    if (kind === 'partial' || kind === 'stale') label.classList.add('warn');
    if (kind === 'error') label.classList.add('error');
    if (detail) label.title = detail;
    else label.removeAttribute('title');
  }

  function updateUpdatedLabel(snapshot) {
    if (!snapshot) {
      setText('#updated', '正在连接采集器…');
      return;
    }
    const time = formatTime(snapshot.generated_at);
    setText('#updated', `更新于 ${time}${snapshot.snapshot_age_seconds !== undefined ? ` · ${formatDuration(snapshot.snapshot_age_seconds)} 前` : ''}`);
  }

  function qualityValue(item, formatter) {
    if (!item || item.value === null || item.value === undefined) return '--';
    if (item.quality === 'unsupported' || item.quality === 'error' || item.quality === 'permission_denied') return '--';
    return formatter(item.value);
  }

  function setMetricTab(metric, item, unit) {
    const button = $(`[data-metric="${metric}"]`);
    if (!button) return;
    const valueNode = button.querySelector('b');
    const unitNode = valueNode?.querySelector('em');
    let value = '--';
    let displayUnit = unit;
    if (item && item.value !== null && item.value !== undefined && !['unsupported', 'error', 'permission_denied'].includes(item.quality)) {
      if (metric === 'network') {
        const rate = formatRate(item.value);
        value = rate.value;
        displayUnit = rate.unit;
      } else if (metric === 'temp') {
        value = Number(item.value).toFixed(Number(item.value) % 1 ? 1 : 0);
      } else {
        value = Number(item.value).toFixed(Number(item.value) < 10 ? 1 : 0).replace(/\.0$/, '');
      }
    }
    if (valueNode) {
      if (valueNode.firstChild) valueNode.firstChild.nodeValue = value;
      else valueNode.prepend(document.createTextNode(value));
    }
    if (unitNode) unitNode.textContent = displayUnit;
    button.dataset.quality = item?.quality || 'unsupported';
    const qualityNote = item?.quality === 'stale' ? '数据已过期' : item?.quality === 'warming_up' ? '正在采集' : item?.quality && item.quality !== 'ok' ? '数据暂缺' : '';
    button.setAttribute('aria-label', `${button.querySelector('span')?.textContent || metric} ${value}${displayUnit}${qualityNote ? `，${qualityNote}` : ''}`);
  }

  function metricDetail(metric, snapshot) {
    const metrics = snapshot?.metrics || {};
    if (metric === 'cpu') {
      const load = (key) => metrics[key]?.value;
      const values = [load('load.1m'), load('load.5m'), load('load.15m')];
      return values.every((value) => Number.isFinite(Number(value)))
        ? `系统负载 ${values.map((value) => formatNumber(value, 2)).join(' / ')}`
        : '系统负载暂不可用';
    }
    if (metric === 'memory') {
      const used = formatBytes(metrics['memory.used_bytes']?.value);
      const total = formatBytes(metrics['memory.total_bytes']?.value);
      return used.value === '--' || total.value === '--' ? '内存读数暂不可用' : `已用 ${used.value} ${used.unit} / 共 ${total.value} ${total.unit}`;
    }
    if (metric === 'temp') {
      const temp = formatTemperature(metrics['temperature.cpu_c']?.value);
      const fan = metrics['fan.rpm'];
      return fan?.value !== null && fan?.value !== undefined ? `CPU ${temp}，风扇 ${fan.value} rpm` : `CPU ${temp}，风扇转速未提供`;
    }
    const rx = formatRate(metrics['network.rx_bytes_per_sec']?.value);
    const tx = formatRate(metrics['network.tx_bytes_per_sec']?.value);
    return `当前接收 ${rx.value} ${rx.unit}，发送 ${tx.value} ${tx.unit}`;
  }

  function renderDevice(snapshot) {
    const device = snapshot.device || {};
    setText('#device-name', device.hostname || '设备管家');
    setText('#device-state', snapshot.attention?.title || '正在采集');
    setText('#device-uptime', `已运行 ${formatDuration(device.uptime_seconds)}`);
    setText('#device-firmware', device.firmware ? `固件 ${device.firmware}` : '固件未提供');
    setText('#device-caption', device.model ? `${device.model} · 双盘位家庭存储` : '双盘位家庭存储');
  }

  function renderAttention(snapshot) {
    const attention = snapshot.attention || { level: 'warn', title: '正在采集', detail: '正在等待第一轮采集。' };
    const row = $('#attention');
    row.classList.remove('warn', 'error');
    if (attention.level === 'warn') row.classList.add('warn');
    if (attention.level === 'error') row.classList.add('error');
    const icon = $('#attention-icon');
    if (icon) icon.setAttribute('href', attention.level === 'ok' ? '#i-check' : '#i-alert');
    setText('#attention-text', attention.detail || attention.title || '正在采集');
    setText('#attention-note', attention.level === 'ok' ? '本地监测结果' : '需要关注');
  }

  function renderMetrics(snapshot) {
    const metrics = snapshot.metrics || {};
    setMetricTab('cpu', metrics['cpu.busy_pct'], '%');
    setMetricTab('memory', metrics['memory.used_pct'], '%');
    setMetricTab('temp', metrics['temperature.cpu_c'], '°C');
    setMetricTab('network', metrics['network.rx_bytes_per_sec'], 'MiB/s');
    setText('#resource-updated', `${formatTime(snapshot.generated_at)} 更新`);
    setText('#metric-detail', metricDetail(state.metric, snapshot));
  }

  function renderDiskRow(disk, compact = false) {
    const row = templates.disk.content.firstElementChild.cloneNode(true);
    const size = formatBytes(disk.size_bytes);
    const label = disk.slot ? `硬盘 ${disk.slot}` : (disk.dev || '硬盘');
    const type = disk.type || '磁盘';
    setText(row.querySelector('.disk-name'), label);
    setText(row.querySelector('.disk-meta'), compact ? `${type} · ${size.value} ${size.unit}` : `${type} · ${size.value} ${size.unit}${disk.model ? ` · ${disk.model}` : ''}`);
    setText(row.querySelector('.disk-temp'), disk.temperature_c === null || disk.temperature_c === undefined ? '温度未提供' : formatTemperature(disk.temperature_c));
    const health = disk.health === 'normal' ? '正常' : disk.health === 'warning' ? '注意' : disk.health === 'error' ? '异常' : '健康未提供';
    const healthNode = row.querySelector('.disk-health');
    setText(healthNode, health);
    healthNode.classList.toggle('unknown', disk.health !== 'normal');
    return row;
  }

  function renderStorage(snapshot) {
    const storage = snapshot.storage;
    const pill = $('#storage-pill');
    const pagePill = $('#storage-page-pill');
    const overviewDisks = $('#overview-disks');
    const detailDisks = $('#storage-detail-disks');
    overviewDisks.replaceChildren();
    detailDisks.replaceChildren();

    if (!storage) {
      pill.textContent = '不可读';
      pill.className = 'pill error';
      pagePill.textContent = '不可读';
      pagePill.className = 'pill error';
      setText('#storage-used-value', '--');
      setText('#storage-used-unit', '已用');
      setText('#storage-total', '共 --');
      setText('#storage-used-pct', '已使用 --');
      setText('#storage-remaining', '剩余 --');
      setText('#storage-source', '存储数据不可读');
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '存储信息暂不可读，请检查宿主探针与挂载。';
      overviewDisks.append(empty);
      detailDisks.append(empty.cloneNode(true));
      setText('#storage-page-note', '存储信息暂不可读。');
      setText('#storage-page-used-value', '--');
      setText('#storage-page-used-unit', '已用');
      setText('#storage-page-total', '共 --');
      setText('#storage-page-used-pct', '已使用 --');
      setText('#storage-page-remaining', '剩余 --');
      $('#storage-page-meta').replaceChildren();
      return;
    }

    const used = formatBytes(storage.used_bytes);
    const total = formatBytes(storage.total_bytes);
    const remaining = formatBytes(storage.available_bytes);
    const usedPct = Number(storage.used_pct || 0);
    const healthyStorage = storage.quality === 'ok' && ['normal', 'ok'].includes(storage.status);
    const statusText = storage.status_label || (healthyStorage ? '存储池正常' : '需要关注');
    pill.textContent = statusText;
    pill.className = `pill${healthyStorage ? '' : storage.quality === 'stale' ? ' warn' : ' error'}`;
    pagePill.textContent = statusText;
    pagePill.className = pill.className;
    setText('#storage-used-value', used.value);
    setText('#storage-used-unit', `${used.unit} 已用`);
    setText('#storage-total', `共 ${total.value} ${total.unit}`);
    setText('#storage-used-pct', `已使用 ${formatNumber(usedPct, 2)}%`);
    setText('#storage-remaining', `剩余约 ${remaining.value} ${remaining.unit}`);
    setText('#storage-source', storage.source === 'firmware.getPoolInfo' ? '容量来自固件存储池' : `容量来源：${storage.source || '未知'}`);
    const bar = $('#storage-bar');
    const barFill = $('#storage-bar-fill');
    bar.setAttribute('aria-label', `存储池已用 ${formatNumber(usedPct, 2)}%`);
    barFill.style.width = `${Math.min(100, Math.max(0, usedPct))}%`;
    setText('#storage-page-used-value', used.value);
    setText('#storage-page-used-unit', `${used.unit} 已用`);
    setText('#storage-page-total', `共 ${total.value} ${total.unit}`);
    setText('#storage-page-used-pct', `已使用 ${formatNumber(usedPct, 2)}%`);
    setText('#storage-page-remaining', `剩余约 ${remaining.value} ${remaining.unit}`);
    const pageBar = $('#storage-page-bar');
    pageBar.setAttribute('aria-label', `存储池已用 ${formatNumber(usedPct, 2)}%`);
    $('#storage-page-bar-fill').style.width = `${Math.min(100, Math.max(0, usedPct))}%`;
    setText('#storage-page-note', storage.source === 'firmware.getPoolInfo'
      ? '池容量与单块硬盘容量来自固件存储池。当前页面未执行磁盘检测或读写操作。'
      : `池容量来自 ${storage.source || '文件系统统计'}。当前页面未执行磁盘检测或读写操作。`);

    const disks = Array.isArray(storage.disks) ? storage.disks : [];
    disks.slice(0, 2).forEach((disk) => overviewDisks.append(renderDiskRow(disk, true)));
    if (!disks.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '未发现成员盘信息。';
      overviewDisks.append(empty);
    }
    disks.forEach((disk) => {
      const card = document.createElement('article');
      card.className = 'panel wide-panel';
      const head = document.createElement('div');
      head.className = 'large-drive';
      const symbol = document.createElement('i');
      symbol.className = 'disk-symbol';
      symbol.setAttribute('aria-hidden', 'true');
      const text = document.createElement('div');
      const title = document.createElement('h3');
      title.textContent = disk.slot ? `硬盘 ${disk.slot}` : (disk.dev || '硬盘');
      const subtitle = document.createElement('p');
      subtitle.textContent = disk.dev ? `设备 ${disk.dev}` : '成员盘';
      text.append(title, subtitle);
      const status = document.createElement('span');
      status.className = 'pill';
      status.style.marginLeft = 'auto';
      const health = disk.health === 'normal' ? '正常' : disk.health === 'warning' ? '注意' : disk.health === 'error' ? '异常' : '健康未提供';
      status.textContent = health;
      if (disk.health !== 'normal') status.classList.add('warn');
      head.append(symbol, text, status);
      card.append(head);
      const size = formatBytes(disk.size_bytes);
      const rows = [
        ['容量 / 类型', `${size.value} ${size.unit} / ${disk.type || '未知'}`],
        ['型号', disk.model || '未提供'],
        ['温度', disk.temperature_c === null || disk.temperature_c === undefined ? '未提供' : formatTemperature(disk.temperature_c)],
        ['使用状态', disk.state === 'inuse' ? '存储池成员' : (disk.state || '未知')],
        ['健康信息', disk.health === 'normal' ? '固件报告正常' : '未提供'],
      ];
      rows.forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'detail-kv';
        const name = document.createElement('span');
        name.textContent = label;
        const val = document.createElement('b');
        val.textContent = value;
        row.append(name, val);
        card.append(row);
      });
      detailDisks.append(card);
    });
    if (!disks.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '未发现成员盘信息。';
      detailDisks.append(empty);
    }

    const meta = $('#storage-page-meta');
    meta.replaceChildren();
    const metaRows = [
      ['存储池类型', storage.pool_type || '未提供'],
      ['成员盘', `${storage.disk_count ?? disks.length} 块`],
      ['最近采集', storage.sampled_at ? formatShortTime(storage.sampled_at) : '未提供'],
      ['容量来源', storage.source === 'firmware.getPoolInfo' ? '固件存储池' : (storage.source || '未提供')],
    ];
    metaRows.forEach(([label, value]) => {
      const row = document.createElement('div');
      row.className = 'detail-kv';
      const name = document.createElement('span');
      name.textContent = label;
      const val = document.createElement('b');
      val.textContent = value;
      row.append(name, val);
      meta.append(row);
    });
  }

  function serviceState(service) {
    const active = service.active_state || 'unknown';
    const sub = service.sub_state || 'unknown';
    if (active === 'active' && sub === 'running') return { text: '运行中', className: '' };
    if (active === 'active' && sub === 'exited') return { text: '已就绪', className: '' };
    if (active === 'failed') return { text: '异常', className: 'error' };
    if (active === 'inactive') return { text: '已停止', className: 'warn' };
    if (service.load_state === 'not-found') return { text: '未安装', className: 'warn' };
    return { text: active === 'unknown' ? '未提供' : active, className: 'warn' };
  }

  function makeServiceRow({ label, meta, stateText, stateClass, icon, detailKey }) {
    const row = templates.service.content.firstElementChild.cloneNode(true);
    row.dataset.detail = detailKey;
    setText(row.querySelector('.service-name'), label);
    setText(row.querySelector('.service-meta'), meta || '');
    setText(row.querySelector('.state-text'), stateText || '未提供');
    row.querySelector('.service-icon use').setAttribute('href', icon || '#i-service');
    const stateNode = row.querySelector('.service-state');
    const dot = row.querySelector('.status-dot');
    stateNode.classList.toggle('warn', stateClass === 'warn');
    stateNode.classList.toggle('error', stateClass === 'error');
    dot.classList.toggle('warn', stateClass === 'warn');
    dot.classList.toggle('error', stateClass === 'error');
    return row;
  }

  function renderServices(snapshot) {
    const services = snapshot.services || {};
    const docker = services.docker || {};
    const containers = Array.isArray(services.containers) ? services.containers : (docker.containers || []);
    const system = services.system || {};
    const running = Number(docker.containers_running || containers.filter((item) => item.state === 'running').length || 0);
    const dockerState = docker.available ? (docker.stale ? { text: '数据已过期', className: 'warn' } : { text: '运行中', className: '' }) : { text: '不可用', className: 'error' };
    const dockerMeta = docker.available
      ? `${docker.version || 'Docker'} · ${running} 个容器运行中${docker.stale ? ' · 数据已过期' : ''}`
      : (docker.reason || 'Docker Engine 不可用');

    const overview = $('#overview-services');
    overview.replaceChildren();
    overview.append(makeServiceRow({
      label: 'Docker 引擎',
      meta: dockerMeta,
      stateText: dockerState.text,
      stateClass: dockerState.className,
      icon: '#i-box',
      detailKey: 'docker',
    }));
    containers.filter((item) => item.state === 'running').slice(0, 3).forEach((container) => {
      overview.append(makeServiceRow({
        label: container.name,
        meta: container.image || '容器服务',
        stateText: container.health === 'unhealthy' ? '异常' : '运行中',
        stateClass: container.health === 'unhealthy' ? 'error' : '',
        icon: '#i-chip',
        detailKey: `container:${container.id}`,
      }));
    });
    if (!containers.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = docker.available ? '当前没有发现容器。' : 'Docker Engine 不可用，容器状态暂时无法读取。';
      overview.append(empty);
    }

    const count = $('#services-count');
    count.textContent = docker.available ? `${running} 个运行中` : 'Docker 不可用';
    count.className = `pill${docker.available ? '' : ' error'}`;

    const containerList = $('#services-containers');
    containerList.replaceChildren();
    containerList.append(makeServiceRow({
      label: 'Docker 引擎',
      meta: dockerMeta,
      stateText: dockerState.text,
      stateClass: dockerState.className,
      icon: '#i-box',
      detailKey: 'docker',
    }));
    containers.forEach((container) => {
      const status = container.state === 'running'
        ? (container.health === 'unhealthy' ? '异常' : '运行中')
        : container.state === 'exited' ? '已停止' : (container.state || '未知');
      const stateClass = container.health === 'unhealthy' ? 'error' : container.state === 'running' ? '' : 'warn';
      containerList.append(makeServiceRow({
        label: container.name,
        meta: `${container.image || '容器'}${container.status ? ` · ${container.status}` : ''}`,
        stateText: status,
        stateClass,
        icon: '#i-chip',
        detailKey: `container:${container.id}`,
      }));
    });
    if (!containers.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = docker.available ? '当前没有发现容器。' : 'Docker Engine 不可用，容器状态暂时无法读取。';
      containerList.append(empty);
    }

    const systemList = $('#services-system');
    systemList.replaceChildren();
    if (!system.available) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '系统服务数据未接入。当前版本优先保证 Linux 指标、固件存储和 Docker 状态。';
      systemList.append(empty);
    } else {
      (system.services || []).forEach((service) => {
        const status = serviceState(service);
        const stale = system.stale === true;
        systemList.append(makeServiceRow({
          label: service.label || service.unit,
          meta: `${service.unit}${stale ? ' · 数据已过期' : ''}`,
          stateText: stale ? `${status.text} · 已过期` : status.text,
          stateClass: stale ? 'warn' : status.className,
          icon: service.unit === 'nginx.service' ? '#i-service' : '#i-grid',
          detailKey: `system:${service.unit}`,
        }));
      });
      if (!(system.services || []).length) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = '没有可显示的系统服务。';
        systemList.append(empty);
      }
    }
  }

  function renderEvents(events) {
    const list = $('#events-list');
    list.replaceChildren();
    if (!events.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '开始监测后会记录本地事件。';
      list.append(empty);
      return;
    }
    events.forEach((event) => {
      const row = templates.event.content.firstElementChild.cloneNode(true);
      const resolved = event.state === 'resolved';
      const timestamp = event.resolved_at || event.last_seen_at || event.opened_at;
      setText(row.querySelector('time'), formatShortTime(timestamp));
      setText(row.querySelector('.event-title'), event.title);
      setText(row.querySelector('.event-detail'), event.detail);
      setText(row.querySelector('.event-state'), resolved ? `已恢复 · ${formatShortTime(event.resolved_at)}` : `进行中 · 首次 ${formatShortTime(event.opened_at)}`);
      row.querySelector('.event-icon').classList.add(resolved ? 'resolved' : 'open');
      list.append(row);
    });
  }

  async function loadEvents() {
    try {
      const payload = await fetchJson('events?limit=50');
      state.events = payload.events || [];
      renderEvents(state.events);
    } catch (error) {
      renderEvents([]);
      setText('#events-note', '事件记录暂时不可读，采集恢复后会重新出现。');
    }
  }

  function configRangeLabel() {
    return rangeConfig[state.range]?.label || state.range;
  }

  function chartAxisLabels() {
    const now = new Date();
    const timeOnly = new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
    if (state.range === '1h') {
      return [
        timeOnly.format(new Date(now.getTime() - 3600 * 1000)),
        timeOnly.format(new Date(now.getTime() - 1800 * 1000)),
        '现在',
      ];
    }
    if (state.range === '7d') {
      return [
        new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(now.getTime() - 7 * 86400 * 1000)),
        new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(now.getTime() - 3.5 * 86400 * 1000)),
        '现在',
      ];
    }
    return [
      `昨天 ${timeOnly.format(new Date(now.getTime() - 86400 * 1000))}`,
      `今天 ${timeOnly.format(new Date(now.getTime() - 12 * 3600 * 1000))}`,
      '现在',
    ];
  }

  function clearChart() {
    $('#chart-line').setAttribute('d', '');
    $('#chart-area').setAttribute('d', '');
    const end = $('#chart-end');
    end.setAttribute('cx', 493);
    end.setAttribute('cy', 132);
    end.style.display = 'none';
  }

  function drawChart() {
    const titleNode = $('#chart-subtitle');
    const config = metricConfig[state.metric];
    const history = state.history;
    const [axisStart, axisMid, axisEnd] = chartAxisLabels();
    setText('#time-start', axisStart);
    setText('#time-mid', axisMid);
    setText('#time-end', axisEnd);
    setText('#metric-detail', metricDetail(state.metric, state.snapshot));
    if (!history || !Array.isArray(history.buckets) || history.buckets.length === 0) {
      titleNode.textContent = '开始监测后会记录趋势';
      titleNode.classList.add('empty');
      clearChart();
      setText('#axis-top', state.metric === 'cpu' || state.metric === 'memory' ? 100 : '--');
      setText('#axis-mid', state.metric === 'cpu' || state.metric === 'memory' ? 50 : '--');
      $('.plot').setAttribute('aria-label', `${config.title}暂无历史数据`);
      return;
    }
    titleNode.classList.remove('empty');
    const buckets = history.buckets;
    let values = buckets.map((item) => Number(item.avg)).filter((value) => Number.isFinite(value));
    if (!values.length) {
      titleNode.textContent = '开始监测后会记录趋势';
      titleNode.classList.add('empty');
      clearChart();
      return;
    }
    let maxValue = 100;
    if (state.metric === 'temp') maxValue = Math.max(60, Math.ceil(Math.max(...values) / 10) * 10);
    if (state.metric === 'network') maxValue = Math.max(1, Math.ceil(Math.max(...values) * 1.2));
    const requestedFrom = new Date(history.requested_from || buckets[0].start).getTime();
    const requestedTo = new Date(history.requested_to || buckets[buckets.length - 1].end).getTime();
    const span = Math.max(1, requestedTo - requestedFrom);
    const x = (timestamp) => 25 + ((timestamp - requestedFrom) / span) * 468;
    const y = (value) => 132 - (Math.max(0, Math.min(maxValue, value)) / maxValue) * 120;
    const segments = [];
    let current = null;
    let previousEnd = 0;
    for (const bucket of buckets) {
      const start = new Date(bucket.start).getTime();
      const end = new Date(bucket.end).getTime();
      const value = Number(bucket.avg);
      if (!Number.isFinite(start) || !Number.isFinite(value)) continue;
      if (current === null || bucket.boot_id !== current.bootId || (previousEnd && start - previousEnd > (end - start) * 1.6)) {
        current = { bootId: bucket.boot_id, points: [] };
        segments.push(current);
      }
      current.points.push({ x: x(start + (end - start) / 2), y: y(value), value });
      previousEnd = end;
    }
    const lineParts = [];
    const areaParts = [];
    for (const segment of segments) {
      if (!segment.points.length) continue;
      const line = segment.points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
      lineParts.push(line);
      const first = segment.points[0];
      const last = segment.points[segment.points.length - 1];
      const rest = segment.points
        .slice(1)
        .map((point) => `L${point.x.toFixed(1)},${point.y.toFixed(1)}`)
        .join(' ');
      areaParts.push(
        `M${first.x.toFixed(1)},132 L${first.x.toFixed(1)},${first.y.toFixed(1)} ${rest} L${last.x.toFixed(1)},132 Z`,
      );
    }
    $('#chart-line').setAttribute('d', lineParts.join(' '));
    $('#chart-area').setAttribute('d', areaParts.join(' '));
    const lastSegment = segments[segments.length - 1];
    const lastPoint = lastSegment?.points[lastSegment.points.length - 1];
    const end = $('#chart-end');
    if (lastPoint) {
      end.style.display = '';
      end.setAttribute('cx', lastPoint.x.toFixed(1));
      end.setAttribute('cy', lastPoint.y.toFixed(1));
    }
    setText('#axis-top', maxValue);
    setText('#axis-mid', maxValue / 2);
    let subtitle = config.title;
    if (history.available_from && history.range === state.range) {
      const recorded = (Date.now() - new Date(history.available_from).getTime()) / 1000;
      if (recorded < rangeConfig[state.range].seconds * 0.85) subtitle += ` · 已记录 ${formatDuration(recorded)}`;
    }
    titleNode.textContent = subtitle;
    $('.plot').setAttribute('aria-label', `${config.title}最近${configRangeLabel()}趋势，当前${formatNumber(values[values.length - 1], 1)}`);
  }

  async function loadHistory(force = false) {
    const seq = ++state.historySeq;
    const config = metricConfig[state.metric];
    const requestKey = `${state.metric}:${state.range}`;
    if (!force && state.history && state.history.metric === config.key && state.history.range === state.range) {
      drawChart();
      return;
    }
    try {
      const history = await fetchJson(`history?metric=${encodeURIComponent(config.key)}&range=${encodeURIComponent(state.range)}`);
      if (seq !== state.historySeq) return;
      state.history = history;
      state.history.requestKey = requestKey;
      drawChart();
    } catch (error) {
      if (seq !== state.historySeq) return;
      state.history = null;
      drawChart();
    }
  }

  function renderSnapshot(snapshot) {
    state.snapshot = snapshot;
    state.snapshotAge = snapshot.snapshot_age_seconds;
    renderDevice(snapshot);
    renderAttention(snapshot);
    renderMetrics(snapshot);
    renderStorage(snapshot);
    renderServices(snapshot);
    updateUpdatedLabel(snapshot);
    const collection = snapshot.collection?.state || 'ok';
    if (collection === 'ok') setConnection('ok');
    else if (collection === 'warming_up') setConnection('starting');
    else setConnection(collection === 'stale' ? 'stale' : 'partial', snapshot.collection?.warnings?.join('；'));
    const currentHistory = state.history
      && state.history.metric === metricConfig[state.metric].key
      && state.history.range === state.range;
    if (currentHistory) {
      drawChart();
    } else {
      $('#chart-subtitle').textContent = '加载趋势…';
      $('#chart-subtitle').classList.add('empty');
      clearChart();
    }
  }

  function scheduleNext(delay) {
    clearTimeout(state.pollTimer);
    if (document.visibilityState !== 'visible') return;
    state.pollTimer = setTimeout(() => loadSnapshot(), delay);
  }

  async function loadSnapshot(manual = false) {
    if (state.inFlight) return;
    state.inFlight = true;
    try {
      const snapshot = await fetchJson('snapshot');
      state.retryDelay = 5000;
      renderSnapshot(snapshot);
      if (!state.history || state.history.metric !== metricConfig[state.metric].key || state.history.range !== state.range) loadHistory();
      if (state.page === 'events') loadEvents();
      if (manual) toast('已读取最新快照');
      scheduleNext(5000);
    } catch (error) {
      if (state.snapshot) {
        setConnection(error.status === 503 ? 'stale' : 'error', error.message);
        setText('#updated', `上次更新 ${formatTime(state.snapshot.generated_at)}`);
      } else {
        setConnection(error.status === 503 ? 'starting' : 'error', error.message);
        setText('#updated', error.status === 503 ? '采集器尚未生成快照' : '无法连接采集器');
      }
      if (manual) toast(error.message || '刷新失败');
      scheduleNext(state.retryDelay);
      state.retryDelay = Math.min(30000, state.retryDelay * 2);
    } finally {
      state.inFlight = false;
    }
  }

  function detailRow(label, value) {
    const row = document.createElement('div');
    row.className = 'detail-kv';
    const name = document.createElement('span');
    name.textContent = label;
    const val = document.createElement('b');
    val.textContent = value === null || value === undefined || value === '' ? '未提供' : value;
    row.append(name, val);
    return row;
  }

  function openDetail(key) {
    const snapshot = state.snapshot;
    if (!snapshot) return;
    const dialog = $('#detail-dialog');
    const content = $('#dialog-content');
    content.replaceChildren();
    let title = '详情';
    let rows = [];
    let note = '';
    if (key === 'source') {
      title = '数据从哪里来';
      const source = snapshot.capabilities?.storage_source || '未知';
      rows = [
        ['资源', 'Linux /proc 与 /sys'],
        ['存储', source === 'firmware.getPoolInfo' ? '固件存储池' : source],
        ['服务', snapshot.services?.system?.available ? '本地 Docker 与 systemd 探针' : '本地 Docker 探针'],
        ['趋势', 'NAS 本地 SQLite，每 5 秒采样'],
        ['宿主探针', snapshot.provenance?.host_sampled_at ? formatShortTime(snapshot.provenance.host_sampled_at) : '尚未提供'],
      ];
      note = '所有数据由这台 NAS 本地采集，不经过云端。状态、趋势和事件保存在本机。';
    } else if (key === 'docker') {
      title = 'Docker 引擎';
      const docker = snapshot.services?.docker || {};
      rows = [
        ['状态', docker.available ? '可用' : '不可用'],
        ['版本', docker.version || '未提供'],
        ['API', docker.api_version || '未提供'],
        ['运行容器', `${docker.containers_running ?? 0} 个`],
        ['最近采集', docker.sampled_at ? formatShortTime(docker.sampled_at) : '未提供'],
      ];
      note = docker.available ? 'Docker Engine 状态来自宿主只读探针。容器运行中不代表业务健康。' : `Docker 不可用：${docker.reason || '未提供原因'}`;
    } else if (key.startsWith('container:')) {
      const id = key.slice('container:'.length);
      const container = (snapshot.services?.containers || []).find((item) => item.id === id);
      if (container) {
        title = container.name;
        const healthLabel = {
          healthy: '通过',
          unhealthy: '异常',
          starting: '检查中',
          not_provided: '未提供',
        }[container.health] || container.health || '未提供';
        rows = [
          ['容器 ID', container.id],
          ['镜像', container.image || '未提供'],
          ['容器状态', container.state || '未知'],
          ['运行摘要', container.status || '未提供'],
          ['业务健康', healthLabel],
        ];
        note = '“运行中”只说明容器进程处于运行状态，不代表业务健康检查已通过。';
      }
    } else if (key.startsWith('system:')) {
      const unit = key.slice('system:'.length);
      const service = (snapshot.services?.system?.services || []).find((item) => item.unit === unit);
      if (service) {
        title = service.label || service.unit;
        const status = serviceState(service);
        rows = [
          ['单元', service.unit],
          ['状态', status.text],
          ['ActiveState', service.active_state || '未提供'],
          ['SubState', service.sub_state || '未提供'],
          ['启动类型', service.file_state || '未提供'],
        ];
        note = service.active_state === 'active' && service.sub_state === 'exited'
          ? '一次性任务完成后可以处于 active/exited，不应据此判断服务异常。'
          : '系统服务状态来自宿主只读探针，仅覆盖固定白名单。';
      }
    }
    if (!rows.length) return;
    setText('#dialog-title', title);
    rows.forEach(([label, value]) => content.append(detailRow(label, value)));
    if (note) {
      const paragraph = document.createElement('p');
      paragraph.textContent = note;
      content.append(paragraph);
    }
    dialog.showModal();
  }

  function selectMetric(button, focus = false) {
    const metric = button.dataset.metric;
    if (!metricConfig[metric]) return;
    state.metric = metric;
    $$('[data-metric]').forEach((tab) => {
      const active = tab === button;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    $('#resource-chart').setAttribute('aria-labelledby', button.id);
    if (focus) button.focus();
    state.history = null;
    $('#chart-subtitle').textContent = '加载趋势…';
    $('#chart-subtitle').classList.add('empty');
    clearChart();
    setText('#metric-detail', metricDetail(metric, state.snapshot));
    loadHistory();
  }

  function bindEvents() {
    $$('[data-page]').forEach((button) => button.addEventListener('click', () => go(button.dataset.page)));
    $$('[data-go]').forEach((button) => button.addEventListener('click', () => go(button.dataset.go)));
    $('#back').addEventListener('click', () => go('overview'));
    $('#refresh').addEventListener('click', async () => {
      state.history = null;
      await loadSnapshot(true);
      if (state.page === 'events') loadEvents();
    });
    document.addEventListener('click', (event) => {
      const target = event.target.closest('[data-detail]');
      if (target) {
        event.preventDefault();
        openDetail(target.dataset.detail);
      }
    });
    const tabs = $$('[data-metric]');
    tabs.forEach((button, index) => {
      button.addEventListener('click', () => selectMetric(button));
      button.addEventListener('keydown', (event) => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault();
        selectMetric(tabs[next], true);
      });
    });
    $$('[data-range]').forEach((button) => {
      button.addEventListener('click', () => {
        state.range = button.dataset.range;
        state.history = null;
        $$('[data-range]').forEach((item) => {
          const active = item === button;
          item.classList.toggle('active', active);
          item.setAttribute('aria-pressed', String(active));
        });
        $('#chart-subtitle').textContent = '加载趋势…';
        $('#chart-subtitle').classList.add('empty');
        clearChart();
        loadHistory();
      });
    });
    $('#close-dialog').addEventListener('click', () => $('#detail-dialog').close());
    $('#dialog-done').addEventListener('click', () => $('#detail-dialog').close());
    $('#detail-dialog').addEventListener('click', (event) => {
      if (event.target !== event.currentTarget) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) {
        event.currentTarget.close();
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') loadSnapshot();
      else clearTimeout(state.pollTimer);
    });
    setText('#time-end', '现在');
    setText('#metric-detail', metricDetail('cpu', null));
  }

  bindEvents();
  loadSnapshot();
})();
