/* 控制台前端逻辑：所有数据通过 /api/status 拿，不再由后端拼 HTML */

const OUTCOME_LABEL = {
  ok: '成功', cached: '缓存', skipped: '跳过',
  empty: '空题', error: '失败', denied: '拒绝'
};

/* 首次加载要把输入框也填上；之后每 5 秒只刷新数字和列表，
   不能碰输入框，否则你正在粘贴 Key 会被冲掉 */
let filled = false;
let HAS_KEY = false;   // 当前是否已保存 Key，用于区分「没填」和「要清除」

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function setText(id, v) {
  const el = document.getElementById(id);
  if (el) el.textContent = v;
}

function renderRecent(list) {
  const box = document.getElementById('recent');
  if (!list || !list.length) {
    box.innerHTML = '<div class="rempty">还没有收到任何请求</div>';
    return;
  }
  box.innerHTML = list.map(it => {
    const label = OUTCOME_LABEL[it.outcome] || it.outcome;
    return '<div class="rrow">'
      + '<span class="rt">' + esc(it.t) + '</span>'
      + '<span class="rb ' + esc(it.outcome) + '">' + esc(label) + '</span>'
      + '<span class="ri">'
      + '<span class="rtitle">' + (esc(it.title) || '(空)') + '</span>'
      + '<span class="rmsg">' + esc(it.type) + ' · ' + esc(it.ms) + 'ms · '
      + esc(it.message) + '</span>'
      + '</span></div>';
  }).join('');
}

/* 缓存题目列表。
   和「最近请求」不同：那是内存里的 20 条流水，重启就没；
   这是 SQLite 里真实存下来的题，重启还在。 */
let CQ = '';          // 当前搜索词
let cTimer = null;    // 输入防抖

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
    + p(d.getHours()) + ':' + p(d.getMinutes());
}

function renderCache(j) {
  const box = document.getElementById('clist');
  const tip = document.getElementById('chint');
  const items = j.items || [];
  const total = j.total || 0;

  if (!items.length) {
    box.innerHTML = '<div class="rempty">'
      + (CQ ? '没有匹配的题目' : '还没有缓存题目') + '</div>';
    tip.textContent = CQ ? '没有匹配「' + CQ + '」的题目' : '共 0 道';
    return;
  }

  tip.textContent = CQ
    ? '匹配 ' + total + ' 道'
    : '共 ' + total + ' 道' + (total > items.length ? '，显示最近 ' + items.length + ' 道' : '');

  box.innerHTML = items.map(it =>
    '<div class="crow">'
      + '<div class="ctitle">' + esc(it.title) + '</div>'
      + '<div class="canswer">' + esc(it.answer) + '</div>'
      + '<div class="cmeta">命中 ' + (it.hits || 0) + ' 次 · ' + fmtTime(it.updated) + '</div>'
      + '</div>').join('');
}

async function loadCache() {
  try {
    const r = await fetch('/api/cache?limit=100&q=' + encodeURIComponent(CQ),
                          { cache: 'no-store' });
    const j = await r.json();
    if (j.code === 1) renderCache(j);
  } catch (e) {
    /* 服务刚重启的瞬间可能连不上，下一轮轮询自己会补上，不用弹错 */
  }
}

async function loadStatus(full) {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    const j = await r.json();

    document.getElementById('dot').className = 'dot ' + (j.ready ? 'on' : 'off');
    setText('status', j.ready ? '已就绪' : '未配置 API Key');
    HAS_KEY = !!j.has_key;

    setText('s-queries', j.stats.queries);
    setText('s-hits', j.stats.cache_hits);
    setText('s-calls', j.stats.api_calls);
    setText('s-cached', j.stats.cached);
    setText('s-skipped', j.stats.skipped);
    setText('s-empty', j.stats.empty);
    setText('s-errors', j.stats.errors);

    setText('s-model', j.model);
    setText('s-port', j.port);
    setText('s-token', j.token_enabled ? '已启用' : '未启用');

    const d = j.db || {};
    document.getElementById('dbinfo').innerHTML =
      '数据库：<code>' + esc(d.path) + '</code>　' + esc(j.stats.cached)
      + ' 道题　' + ((d.size || 0) / 1024).toFixed(1) + ' KB';

    renderRecent(j.recent);

    // 正在搜索框里打字时不要刷新列表，否则滚动位置和光标都会被打断
    if (document.activeElement !== document.getElementById('cq')) loadCache();

    if (!full && filled) return;   // 轮询时到此为止，不动输入框

    document.getElementById('key').value = '';
    // 脱敏值只放 placeholder，不要填进 value。
    // 填进 value 会有两个后果：一是看起来像"可以编辑的 Key"，二是
    // 用户不动它直接点保存，提交上去的是 "sk-abc***3456" 这种脱敏串。
    document.getElementById('key').placeholder = j.has_key ? j.masked_key : 'sk-...';
    document.getElementById('clearBtn').style.display = j.has_key ? '' : 'none';
    document.getElementById('keyhint').innerHTML = j.has_key
      ? '当前已配置 <code>' + esc(j.masked_key) + '</code>，输入框留空表示保持不变。'
      : '还没有 Key。去 <a href="https://platform.deepseek.com" target="_blank">'
        + 'platform.deepseek.com</a> 注册充值后，把 Key 粘贴到这里保存即可。';
    document.getElementById('base').value = j.base_url || '';
    document.getElementById('tk').value = j.token || '';

    setText('ocsurl', j.ocs.url);
    setText('cfg', j.ocs.config);
    document.getElementById('tokenhint').innerHTML = j.token_enabled
      ? '<label class="mt">访问令牌</label><div class="copy">'
        + '<code id="tkv">' + esc(j.token) + '</code>'
        + '<button class="ghost" onclick="cp(\'tkv\', this)">复制</button></div>'
      : '';
    filled = true;
  } catch (e) {
    document.getElementById('dot').className = 'dot off';
    setText('status', '服务未响应');
  }
}

function cp(id, btn) {
  const t = document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(() => {
    const o = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = o; }, 1200);
  });
}

function adv() {
  const d = document.getElementById('adv');
  const open = d.style.display === 'none';
  d.style.display = open ? 'block' : 'none';
  document.getElementById('advlink').textContent = open ? '收起高级选项' : '高级选项';
}

async function saveKey(btn) {
  const inp = document.getElementById('key'), msg = document.getElementById('kmsg');
  const v = inp.value.trim();
  if (!v) {
    msg.className = 'msg no';
    msg.textContent = HAS_KEY
      ? '输入框是空的。要更换 Key 请粘贴新的；要清除已保存的，点「清除已保存的 Key」。'
      : '请输入 API Key';
    return;
  }
  const o = btn.textContent;
  btn.disabled = true; btn.textContent = '保存中...';
  msg.className = 'msg';

  const payload = { api_key: v };
  const advOpen = document.getElementById('adv').style.display !== 'none';
  if (advOpen) {
    payload.base_url = document.getElementById('base').value.trim();
    payload.token = document.getElementById('tk').value.trim();
  }

  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const j = await r.json();
    if (j.code === 1) {
      msg.className = 'msg ok';
      msg.textContent = j.tested
        ? '已保存，并测试通过。可以开始用了。'
        : '已保存。注意：连通性测试未通过，请检查 Key 或 API 地址是否正确。';
      if (j.tested) setTimeout(() => location.reload(), 1500);
    } else {
      msg.className = 'msg no';
      msg.textContent = '保存失败：' + j.message;
    }
  } catch (e) {
    msg.className = 'msg no';
    msg.textContent = '请求失败：' + e;
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

/* 清除已保存的 Key。
   单独走一个 clear_key 标志，而不是提交空字符串——
   后端把"没传这个字段"当成"不改"，空串要靠猜意图，容易两边理解不一致。 */
async function clearKey(btn) {
  if (!confirm('确定清除已保存的 API Key？\n\n清除后答题不可用，需要重新填写。')) {
    return;
  }
  const msg = document.getElementById('kmsg');
  const o = btn.textContent;
  btn.disabled = true; btn.textContent = '清除中...';
  msg.className = 'msg';
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear_key: true })
    });
    const j = await r.json();
    if (j.code === 1) {
      msg.className = 'msg ok';
      msg.textContent = '已清除，Key 已从 .env 移除。';
      setTimeout(() => location.reload(), 900);
    } else {
      msg.className = 'msg no';
      msg.textContent = '清除失败：' + j.message;
    }
  } catch (e) {
    msg.className = 'msg no';
    msg.textContent = '请求失败：' + e;
  } finally {
    btn.disabled = false; btn.textContent = o;
  }
}

async function go(btn) {
  const out = document.getElementById('out');
  out.className = ''; out.textContent = '思考中...';
  btn.disabled = true;
  const p = new URLSearchParams({
    title: document.getElementById('q').value,
    options: document.getElementById('o').value,
    type: document.getElementById('t').value
  });
  try {
    const r = await fetch('/query?' + p);
    const j = await r.json();
    if (j.code === 1) {
      out.className = 'good';
      out.textContent = '答案：' + j.data.answer;
    } else {
      out.className = 'err';
      out.textContent = '未答上：' + j.message;
    }
    loadStatus(false);   // 立刻把这次请求刷进列表
  } catch (e) {
    out.className = 'err';
    out.textContent = '请求失败：' + e;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('cq').addEventListener('input', e => {
  CQ = e.target.value.trim();
  clearTimeout(cTimer);
  cTimer = setTimeout(loadCache, 250);   // 每敲一个字就查库没必要
});

loadStatus(true);
setInterval(() => loadStatus(false), 5000);
