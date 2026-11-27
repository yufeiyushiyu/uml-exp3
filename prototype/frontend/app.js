/* 物联知图平台前端逻辑
   职责边界：只做"取数—渲染—转发用户意图"，不做权限与参数合法性判断
   （判断在后端，前端校验只用于减少无效请求）。 */

const state = {
  token: localStorage.getItem('iotkg_token') || '',
  user: null,
  network: null,      // vis-network 实例
  nodes: null,        // vis DataSet
  edges: null,        // vis DataSet
  current: null,      // 当前选中的节点详情
  pending: null,      // 待确认的指令
  ws: null,
};

/* ---------------- 网络请求 ---------------- */
async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) { logout(); throw new Error('会话失效，请重新登录'); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const d = data.detail || {};
    throw new Error(d.message || d || `请求失败(${resp.status})`);
  }
  return data;
}

/* ---------------- 登录 ---------------- */
async function login() {
  const account = document.getElementById('account').value.trim();
  const password = document.getElementById('password').value.trim();
  try {
    const res = await api('/api/auth/login', {
      method: 'POST', body: JSON.stringify({ account, password }),
    });
    state.token = res.token;
    state.user = res.user;
    localStorage.setItem('iotkg_token', res.token);
    startApp();
  } catch (e) {
    alert(e.message);
  }
}

function logout() {
  localStorage.removeItem('iotkg_token');
  state.token = '';
  document.getElementById('app').classList.add('hidden');
  document.getElementById('login-mask').classList.remove('hidden');
}

/* ---------------- 启动 ---------------- */
async function startApp() {
  document.getElementById('login-mask').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  try {
    state.user = await api('/api/auth/me');
  } catch (e) { return; }
  document.getElementById('who').textContent =
    `${state.user.name} · ${state.user.role}${state.user.scope ? ' · ' + state.user.scope : ''}`;
  await loadLocations();
  await renderLegend();
  await loadGraph();
  await loadAlarms();
  connectWs();
}

/* ---------------- 图谱 ---------------- */
async function loadLocations() {
  const list = await api('/api/locations');
  const sel = document.getElementById('loc-filter');
  list.forEach((l) => {
    const o = document.createElement('option');
    o.value = l.node_id; o.textContent = l.label;
    sel.appendChild(o);
  });
}

async function renderLegend() {
  const legend = document.getElementById('legend');
  const items = [
    ['#8E9AAF', '位置'], ['#E4A11B', '网关'], ['#2E86AB', '传感器'],
    ['#6A994E', '执行器'], ['#BC4B51', '用户'], ['#C1121F', '告警'],
  ];
  legend.innerHTML = items.map(([c, t]) => `<div><i style="background:${c}"></i>${t}</div>`).join('');
}

async function loadGraph() {
  const loc = document.getElementById('loc-filter').value;
  const type = document.getElementById('type-filter').value;
  const kw = document.getElementById('kw').value.trim();
  const qs = new URLSearchParams({ locationId: loc, entityTypes: type, keyword: kw, limit: 300 });
  const g = await api('/api/graph?' + qs.toString());

  state.nodes = new vis.DataSet(g.nodes.map((n) => ({
    id: n.id, label: n.label, group: n.group, title: n.title,
    color: { background: n.color, border: '#ffffff', highlight: { background: n.color, border: '#1f3a5f' } },
    shape: n.shape,
    font: { color: '#ffffff', size: 14 },
    borderWidth: n.state === 'FAULT' ? 3 : 1,
    borderWidthSelected: 3,
    _raw: n,
  })));
  state.edges = new vis.DataSet(g.edges.map((e) => ({
    id: e.id, from: e.from, to: e.to, label: e.label, arrows: 'to',
    color: { color: '#b9c6d4', highlight: '#2e86ab' },
    font: { size: 10, color: '#7b8794', align: 'middle' },
  })));

  const container = document.getElementById('graph');
  const data = { nodes: state.nodes, edges: state.edges };
  const options = {
    physics: { stabilization: { iterations: 220 }, barnesHut: { springLength: 120 } },
    interaction: { hover: true, dragNodes: true, zoomView: true },
    nodes: { scaling: { min: 12, max: 28 } },
  };
  state.network = new vis.Network(container, data, options);

  // 点选节点 → 拉取详情（对应顺序图 sd01 后半段）
  state.network.on('selectNode', async (p) => {
    const node = state.nodes.get(p.nodes[0]);
    if (node) await showDetail(node.id);
  });

  const stat = document.getElementById('stat');
  stat.textContent = `节点 ${g.nodes.length} · 关系 ${g.edges.length}` + (g.truncated ? ' · 已分区加载' : '');
}

/* ---------------- 节点详情 ---------------- */
async function showDetail(nodeId) {
  const detail = await api('/api/graph/node/' + encodeURIComponent(nodeId));
  state.current = detail;
  const node = detail.node;
  document.getElementById('node-empty').classList.add('hidden');
  document.getElementById('node-detail').classList.remove('hidden');
  document.getElementById('nd-title').textContent = node.label + `（${node.etype}）`;

  // 属性渲染：状态单独做徽标，其余按 kv 展示
  const props = node.props || {};
  const status = node.state || props.status || '';
  const badges = [
    `<div><span>状态</span><span class="badge ${statusClass(status)}">${status || '未知'}</span></div>`,
    `<div><span>节点ID</span>${node.node_id}</div>`,
  ];
  Object.entries(props).forEach(([k, v]) => {
    if (k === 'status') return;
    badges.push(`<div><span>${k}</span>${typeof v === 'object' ? JSON.stringify(v) : v}</div>`);
  });
  document.getElementById('nd-props').innerHTML = badges.join('');

  renderActions(detail);
  renderMetricSelect(detail);
}

function statusClass(s) {
  if (s === 'ONLINE' || s === 'CONNECTED') return 'online';
  if (s === 'FAULT') return 'fault';
  if (s === 'OFFLINE' || s === 'DISCONNECTED') return 'offline';
  return '';
}

/* 动作白名单渲染：只展示后端下发的动作，前端不硬编码任何动作 */
function renderActions(detail) {
  const box = document.getElementById('nd-actions');
  const dev = detail.device;
  if (!dev || !dev.actions || !dev.actions.length) {
    box.innerHTML = '<div class="empty">该节点无可用动作</div>';
    return;
  }
  const canControl = state.user && (state.user.role === 'ADMIN' || state.user.role === 'OPS');
  box.innerHTML = '<h4>可用动作</h4>' + dev.actions.map((a) => {
    const params = Object.entries(a.params || {}).map(([k, rule]) =>
      `<input data-param="${k}" placeholder="${k}（${rule.min ?? ''}~${rule.max ?? ''}）" />`).join('');
    return `<div class="action-item" data-action="${a.actionId}">
      <div class="name">${a.name}<span class="risk">风险 L${a.risk ?? 1}</span></div>
      ${params}
      <button ${canControl ? '' : 'disabled'} data-send="${a.actionId}">
        ${canControl ? '提交指令' : '当前角色无操控权限'}
      </button>
    </div>`;
  }).join('');

  box.querySelectorAll('button[data-send]').forEach((btn) => {
    btn.addEventListener('click', () => submitCommand(dev.device_id, btn.dataset.send, btn));
  });
}

/* 提交指令 → 弹出确认层（体现"待撤销"状态） */
async function submitCommand(deviceId, actionId, btn) {
  const wrap = btn.closest('.action-item');
  const params = {};
  wrap.querySelectorAll('input[data-param]').forEach((i) => {
    if (i.value) params[i.dataset.param] = Number(i.value);
  });
  try {
    const res = await api('/api/commands', {
      method: 'POST', body: JSON.stringify({ deviceId, actionId, params }),
    });
    state.pending = res.commandId;
    document.getElementById('confirm-body').innerHTML =
      `<div class="props"><div><span>设备</span>${deviceId}</div>
       <div><span>动作</span>${actionId}</div>
       <div><span>参数</span>${JSON.stringify(params)}</div>
       <div><span>状态</span>${res.state}</div></div>`;
    document.getElementById('confirm-mask').classList.remove('hidden');
  } catch (e) {
    alert('指令被拒绝：' + e.message);
  }
}

async function confirmSend() {
  const id = state.pending;
  document.getElementById('confirm-mask').classList.add('hidden');
  if (!id) return;
  try {
    const res = await api(`/api/commands/${id}/confirm`, { method: 'POST' });
    const text = res.success
      ? `执行成功，属性更新为 ${JSON.stringify(res.effect)}`
      : `执行失败：${res.message}`;
    alert(text);
    loadCommands();
    if (state.current) showDetail(state.current.node.node_id);
  } catch (e) {
    alert(e.message);
  } finally {
    state.pending = null;
  }
}

/* ---------------- 历史曲线 ---------------- */
function renderMetricSelect(detail) {
  const sel = document.getElementById('metric-select');
  const dev = detail.device;
  sel.innerHTML = '';
  if (!dev || !dev.metrics || !dev.metrics.length) {
    document.getElementById('chart').getContext('2d').clearRect(0, 0, 360, 150);
    return;
  }
  dev.metrics.forEach((m) => {
    const o = document.createElement('option');
    o.value = m.key; o.textContent = `${m.name}（${m.unit}）`;
    sel.appendChild(o);
  });
  sel.onchange = () => drawChart(dev.device_id, sel.value);
  drawChart(dev.device_id, sel.value);
}

async function drawChart(deviceId, metric) {
  const data = await api(`/api/history?deviceId=${deviceId}&metric=${metric}&agg=2`);
  const canvas = document.getElementById('chart');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const pts = data.points.slice(-40);
  if (pts.length < 2) { ctx.fillStyle = '#97a3b0'; ctx.fillText('暂无足够数据', 12, 24); return; }
  const vs = pts.map((p) => p.value);
  const min = Math.min(...vs), max = Math.max(...vs);
  const pad = 24;
  ctx.strokeStyle = '#e3e9f0';
  ctx.beginPath(); ctx.moveTo(pad, H - pad); ctx.lineTo(W - 8, H - pad); ctx.stroke();
  ctx.strokeStyle = '#2e86ab'; ctx.lineWidth = 2; ctx.beginPath();
  pts.forEach((p, i) => {
    const x = pad + i * (W - pad - 10) / (pts.length - 1);
    const y = H - pad - (max === min ? 0.5 : (p.value - min) / (max - min)) * (H - pad * 2);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = '#5d6b7a'; ctx.font = '11px sans-serif';
  ctx.fillText(`max ${max} / min ${min}`, pad, 14);
}

/* ---------------- 告警 ---------------- */
async function loadAlarms() {
  const list = await api('/api/alarms');
  const box = document.getElementById('alarm-list');
  if (!list.length) { box.innerHTML = '<div class="empty">暂无告警</div>'; return; }
  box.innerHTML = list.map((a) => `
    <div class="alarm-row ${a.level}">
      <b>${a.level}</b> ${a.node_id} · ${a.metric_key} = ${a.value}
      <div class="meta">${a.triggered_at} · 状态 ${a.state}
        ${a.state === 'RAISED' ? `<button data-ack="${a.alarm_id}">确认</button>` : ''}
      </div>
    </div>`).join('');
  box.querySelectorAll('button[data-ack]').forEach((b) =>
    b.addEventListener('click', async () => {
      await api(`/api/alarms/${b.dataset.ack}/ack`, { method: 'POST' });
      loadAlarms();
    }));
}

/* ---------------- 指令列表 ---------------- */
async function loadCommands() {
  const list = await api('/api/commands');
  const box = document.getElementById('cmd-list');
  if (!list.length) { box.innerHTML = '<div class="empty">暂无指令记录</div>'; return; }
  box.innerHTML = list.map((c) => `
    <div class="cmd-row">
      <span class="state">${c.state}</span>
      <div><b>${c.action_id}</b> → ${c.device_id}</div>
      <div class="meta">${c.issued_at} · ${c.issued_by} · 重试 ${c.retry_count}</div>
    </div>`).join('');
}

/* ---------------- WebSocket 增量 ---------------- */
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  state.ws = ws;
  ws.onopen = () => setInterval(() => ws.readyState === 1 && ws.send('ping'), 20000);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.event === 'graph.delta' && state.nodes) {
      // 只更新对应节点，不整体重绘——这是"增量更新"的设计要点
      const node = state.nodes.get(msg.data.nodeId);
      if (node) {
        const props = { ...node._raw.props, ...msg.data.props };
        const newRaw = { ...node._raw, props, version: msg.data.version };
        // 注意：必须整体展开旧节点再覆盖字段，否则 vis 会丢弃 color/shape，
        // 节点退回 group 默认配色（这是调试时实际踩过的坑）。
        state.nodes.update({
          ...node,
          title: `${newRaw.label}\n${Object.entries(props).map(([k, v]) => `${k}: ${v}`).join('\n')}`,
          _raw: newRaw,
        });
        if (state.current && state.current.node.node_id === msg.data.nodeId) showDetail(msg.data.nodeId);
      }
    }
    if (msg.event === 'alarm.new') loadAlarms();
    if (msg.event === 'graph.node.added') loadGraph();
  };
  ws.onclose = () => setTimeout(connectWs, 3000);  // 断线自动重连
}

/* ---------------- 事件绑定 ---------------- */
window.addEventListener('DOMContentLoaded', () => {
  document.getElementById('login-btn').onclick = login;
  document.getElementById('password').onkeydown = (e) => { if (e.key === 'Enter') login(); };
  document.getElementById('logout').onclick = logout;
  document.getElementById('reload').onclick = loadGraph;
  document.getElementById('type-filter').onchange = loadGraph;
  document.getElementById('loc-filter').onchange = loadGraph;
  document.getElementById('alarm-refresh').onclick = loadAlarms;
  document.getElementById('confirm-ok').onclick = confirmSend;
  document.getElementById('confirm-cancel').onclick = () => {
    document.getElementById('confirm-mask').classList.add('hidden');
    state.pending = null;
  };
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    ['detail', 'alarm', 'cmd'].forEach((k) =>
      document.getElementById('tab-' + k).classList.toggle('hidden', k !== t.dataset.tab));
    if (t.dataset.tab === 'alarm') loadAlarms();
    if (t.dataset.tab === 'cmd') loadCommands();
  }));

  // 已登录则直接进入
  if (state.token) startApp();
});
