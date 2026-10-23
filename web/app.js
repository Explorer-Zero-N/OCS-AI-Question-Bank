/* 控制台前端逻辑：所有数据通过 /api/status 拿，不再由后端拼 HTML */

let S = null;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function setText(id, v) {
  const el = document.getElementById(id);
  if (el) el.textContent = v;
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const j = await r.json();
    S = j;

    // 顶部状态
    document.getElementById('dot').className = 'dot ' + (j.ready ? 'on' : 'off');
    setText('status', j.ready ? '已就绪' : '未配置 API Key');

    // 统计
    setText('s-queries', j.stats.queries);
    setText('s-hits', j.stats.cache_hits);
    setText('s-calls', j.stats.api_calls);
    setText('s-cached', j.stats.cached);
    setText('s-skipped', j.stats.skipped);
    setText('s-errors', j.stats.errors);

    // 环境
    setText('s-model', j.model);
    setText('s-port', j.port);
    setText('s-token', j.token_enabled ? '已启用' : '未启用');

    // API 设置
    const keyInput = document.getElementById('key');
    keyInput.value = j.masked_key || '';
    document.getElementById('keyhint').innerHTML = j.has_key
      ? '当前已配置 <code>' + esc(j.masked_key) + '</code>，留空则保持不变。'
      : '还没有 Key。去 <a href="https://platform.deepseek.com" target="_blank">'
        + 'platform.deepseek.com</a> 注册充值后，把 Key 粘贴到这里保存即可。';
    document.getElementById('base').value = j.base_url || '';
    document.getElementById('tk').value = j.token || '';

    // 接入 OCS
    setText('ocsurl', j.ocs.url);
    setText('cfg', j.ocs.config);
    document.getElementById('tokenhint').innerHTML = j.token_enabled
      ? '<label class="mt">访问令牌</label><div class="copy">'
        + '<code id="tkv">' + esc(j.token) + '</code>'
        + '<button class="ghost" onclick="cp(\'tkv\', this)">复制</button></div>'
      : '';
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
  if (!v) { msg.className = 'msg no'; msg.textContent = '请输入 API Key'; return; }
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
  } catch (e) {
    out.className = 'err';
    out.textContent = '请求失败：' + e;
  } finally {
    btn.disabled = false;
  }
}

loadStatus();
