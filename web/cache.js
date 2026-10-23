/* 缓存题目页：列表、搜索、逐条删除、一键清空。
   这一页只做这一件事，所以不依赖 app.js，函数自己带一份。 */

let CQ = '';              // 当前搜索词
let SORT = 'updated';     // 当前排序
let UNUSED = false;       // 是否只看没命中过的
let TOTAL = 0;            // 当前结果条数
let FULL_TOTAL = 0;       // 全库条数，清空确认框里用
let timer = null;

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
    + p(d.getHours()) + ':' + p(d.getMinutes());
}

function render(j) {
  const box = document.getElementById('clist');
  const tip = document.getElementById('chint');
  const items = j.items || [];

  TOTAL = j.total || 0;
  FULL_TOTAL = j.all_total || 0;

  // 提示语要说清现在数的是哪一批：
  // 全库、搜索结果、还是"没命中过"的子集。混着显示会看不懂 49 和 3 差在哪。
  let text;
  if (CQ) {
    text = '匹配 ' + TOTAL + ' 道';
  } else if (UNUSED) {
    text = '从没命中过的有 ' + TOTAL + ' 道（全库 ' + FULL_TOTAL + ' 道）';
  } else {
    text = '共 ' + TOTAL + ' 道';
  }
  if (TOTAL > items.length) text += '，显示 ' + items.length + ' 道';

  document.getElementById('clearBtn').disabled = !FULL_TOTAL;

  if (!items.length) {
    let empty;
    if (CQ) empty = '没有匹配的题目';
    else if (UNUSED) empty = '没有没命中过的题 —— 每条缓存都被复用过';
    else empty = '还没有缓存题目';
    box.innerHTML = '<div class="rempty">' + empty + '</div>';
    tip.textContent = text;
    return;
  }

  tip.textContent = text;

  box.innerHTML = items.map(it =>
    '<div class="crow">'
      + '<div class="crowhead">'
      + '<div class="ctitle">' + esc(it.title) + '</div>'
      + '<button class="cdel" onclick="delOne(\'' + esc(it.qhash) + '\', this)">删除</button>'
      + '</div>'
      + '<div class="canswer">' + esc(it.answer) + '</div>'
      + '<div class="cmeta">命中 ' + (it.hits || 0) + ' 次 · ' + fmtTime(it.updated) + '</div>'
      + '</div>').join('');
}

function changeSort(el) {
  SORT = el.value;
  load();
}

function changeUnused(el) {
  UNUSED = el.checked;
  load();
}

async function load() {
  const p = new URLSearchParams({ limit: '500', q: CQ, sort: SORT });
  if (UNUSED) p.set('unused', '1');
  try {
    const r = await fetch('/api/cache?' + p.toString(), { cache: 'no-store' });
    const j = await r.json();
    if (j.code === 1) render(j);
  } catch (e) {
    document.getElementById('chint').textContent = '服务未响应，稍后自动重试';
  }
}

async function delOne(hash, btn) {
  if (!confirm('删除这一条缓存？\n\n删除后这道题下次遇到会重新问模型，会再花一次钱。')) {
    return;
  }
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '删除中';
  try {
    const r = await fetch('/api/cache/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hash: hash })
    });
    const j = await r.json();
    if (j.code !== 1) alert('删除失败：' + (j.message || '未知原因'));
  } catch (e) {
    alert('请求失败：' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    load();          // 不管成功失败都重拉，界面跟库保持一致
  }
}

async function clearAll(btn) {
  const n = FULL_TOTAL || TOTAL;
  if (!n) return;
  // 说明"清的是全库"：搜索状态下人容易以为只清搜到的这些
  if (!confirm('确定清空全部 ' + n + ' 条缓存？\n\n'
             + '不是只清搜索结果，是整个缓存库。\n'
             + '清空后所有题都会重新问模型，费用会重新产生，且无法撤销。')) {
    return;
  }
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '清空中';
  try {
    const r = await fetch('/api/cache/clear', { method: 'POST' });
    const j = await r.json();
    if (j.code === 1) {
      FULL_TOTAL = 0;
      load();
    } else {
      alert('清空失败：' + (j.message || '未知原因'));
    }
  } catch (e) {
    alert('请求失败：' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

document.getElementById('cq').addEventListener('input', e => {
  CQ = e.target.value.trim();
  clearTimeout(timer);
  timer = setTimeout(load, 250);
});

load();
setInterval(load, 10000);   // 刷课期间这个页面开着时能自动跟上
