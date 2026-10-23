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

/* 缓存题目搬到了独立页面 /cache.html。
   主页只留一个入口和一个开关，理由：一屏塞下统计、最近请求、缓存列表，
   刷课的时候根本没法看；而且删除是危险操作，不该和统计混在一起。 */

async function toggleNoCache(cb) {
  const want = cb.checked;
  cb.disabled = true;
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ no_cache: want })
    });
    const j = await r.json();
    if (j.code !== 1) {
      cb.checked = !want;
      alert('设置失败：' + j.message);
    }
  } catch (e) {
    cb.checked = !want;
    alert('请求失败：' + e);
  } finally {
    cb.disabled = false;
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

    // 缓存入口上的数字，刷课的时候能看出在不在往库里存
    setText('cn', j.stats.cached);

    if (!full && filled) return;   // 轮询时到此为止，不动输入框

    document.getElementById('key').value = '';
    // 脱敏值只放 placeholder，不要填进 value。
    // 填进 value 会有两个后果：一是看起来像"可以编辑的 Key"，二是
    // 用户不动它直接点保存，提交上去的是 "sk-abc***3456" 这种脱敏串。
    document.getElementById('key').placeholder = j.has_key ? j.masked_key : 'sk-...';
    document.getElementById('clearBtn').style.display = j.has_key ? '' : 'none';
    document.getElementById('nocache').checked = !!j.no_cache;
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

loadStatus(true);
setInterval(() => loadStatus(false), 5000);
