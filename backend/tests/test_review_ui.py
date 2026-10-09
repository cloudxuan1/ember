"""运行审核台的真实脚本，用可控响应顺序检查切页竞态（无需浏览器或 npm 依赖）。"""

import json
import re
import shutil
import subprocess

import pytest

from app.review import CONSOLE_PAGE


def test_late_responses_do_not_replace_the_current_review_view():
    node = shutil.which("node")
    if node is None:
        pytest.skip("审核台脚本回归需要 Node.js")
    script = re.search(r"<script>(.*?)</script>", CONSOLE_PAGE, re.S).group(1)
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const nodes = new Map(), requests = [];
const document = {querySelector(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    textContent: '', children: [],
    replaceChildren(...children) { this.children = children; },
  });
  return nodes.get(selector);
}};
const context = vm.createContext({document, fetch(path) {
  return new Promise(resolve => requests.push({path, resolve}));
}});
vm.runInContext(source, context); // 同时检查完整页面脚本可执行；最初两条请求保持未完成
// 此测试只关心谁能替换列表，不复刻卡片内部的 DOM 布局。
vm.runInContext('card = reviewedCard = memCard = editCard = x => x; renderBatches = () => {};', context);
const data = id => ({stats: {total: 1, by_batch: {test: 1}}, items: [{id}]});
async function respond(index, body) {
  requests[index].resolve({ok: true, status: 200, json: async () => body});
  await new Promise(resolve => setImmediate(resolve));
}
(async () => {
  assert.equal(requests.length, 2);
  vm.runInContext('setMode("edits")', context);
  assert.equal(requests[2].path, '/review/api/edits');
  await respond(2, data('current-edit'));
  const stats = nodes.get('#stats').textContent;
  await respond(0, data('old-draft'));
  await respond(1, data('old-edit'));
  assert.equal(nodes.get('#list').children[0].id, 'current-edit', '旧草稿响应不能盖住改动页');
  assert.equal(nodes.get('#stats').textContent, stats);

  // 同一视图连续刷新：先发的旧快照最后返回，也不能把已处理的提议重新放回来。
  vm.runInContext('loadEdits(); loadEdits();', context);
  await respond(4, {stats: {total: 0}, items: []});
  await respond(3, data('already-confirmed'));
  assert.equal(nodes.get('#list').children.length, 0, '旧快照不能恢复已经处理的卡片');
  assert.equal(nodes.get('#empty').hidden, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness], input=json.dumps(script), text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
