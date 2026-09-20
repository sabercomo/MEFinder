// 浏览器回归：对测试库副本启动 serve 后运行。需外部安装 playwright；不下载依赖。
// NODE_PATH=<node_modules> CHROME_PATH=<chrome> node scripts/check_body_range_ui.cjs <url> <output-dir>
// 读取真实书对/文本；只模拟模型就绪与提交失败，绝不发起真实对齐。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const [url, output] = process.argv.slice(2);
  assert(url && output, '需要测试服务 URL 和截图目录');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_PATH});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    const errors = [], submissions = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/text-alignment/models', async route => {
      const response = await route.fetch();
      const data = await response.json();
      data.compute = {available: true, provider: 'builtin'};
      data.models.forEach(model => { model.installed = true; model.state = 'installed'; });
      await route.fulfill({json: data});
    });
    await page.route('**/api/text-alignments/start', async route => {
      submissions.push(route.request().postDataJSON());
      await route.fulfill({status: 400, json: {error: '浏览器测试：模拟提交失败，未执行对齐'}});
    });
    await page.goto(url);
    await page.waitForFunction(() => window.MEFinder && MEFinder.works);
    const groups = await (await page.request.get(url + '/api/document-groups')).json();
    const group = groups.document_groups.find(item => item.members.length >= 2);
    assert(group, '测试库需要至少一个包含两本书的作品');
    await page.evaluate(id => MEFinder.works.open(id), group.document_group_id);
    const open = async () => {
      await page.getByRole('button', {name: '正文范围', exact: true}).first().click();
      await page.waitForFunction(() => document.querySelectorAll('.tw-range-book').length === 2 &&
        [...document.querySelectorAll('.tw-range-book')].every(book =>
          book.querySelectorAll('.tw-range-segment input').length >= 4));
    };
    await open();
    const books = page.locator('.tw-range-book');
    const left = books.nth(0), right = books.nth(1);
    const originalBounds = await books.locator('.tw-range-bounds').allTextContents();
    // 右侧目录、未提交输入与浏览位置不能被左侧动作打断。
    await right.locator('summary').click();
    await right.locator('.tw-range-number').fill('123');
    const rightScroll = await right.locator('.tw-range-reading').evaluate(node => {
      node.scrollTop = 150;
      return node.scrollTop;
    });
    await left.locator('.tw-range-segment input').nth(2).check();
    assert.equal(await right.locator('details').getAttribute('open'), '');
    assert.equal(await right.locator('.tw-range-number').inputValue(), '123');
    assert.equal(await right.locator('.tw-range-reading').evaluate(node => node.scrollTop), rightScroll);
    assert.deepEqual(await books.locator('.tw-range-bounds').allTextContents(), originalBounds);
    const leftScroll = await left.locator('.tw-range-reading').evaluate(node => {
      node.scrollTop = node.scrollHeight;
      return node.scrollTop;
    });
    await left.getByRole('button', {name: '设为结尾', exact: true}).click();
    assert.equal(await left.locator('.tw-range-reading').evaluate(node => node.scrollTop), leftScroll);
    assert.equal(await right.locator('.tw-range-bounds').textContent(), originalBounds[1]);
    await left.getByRole('button', {name: '撤销', exact: true}).click();
    assert.deepEqual(await books.locator('.tw-range-bounds').allTextContents(), originalBounds);
    // 取消不提交、不把新边界保留成草稿。
    await left.getByRole('button', {name: '设为结尾', exact: true}).click();
    await page.getByRole('button', {name: '取消', exact: true}).click();
    await open();
    assert.deepEqual(await books.locator('.tw-range-bounds').allTextContents(), originalBounds);
    assert.equal(submissions.length, 0);
    await left.locator('.tw-range-segment input').nth(2).check();
    // 倒置范围显示错误并阻止提交；撤销恢复可提交。
    await left.getByRole('button', {name: '设为结尾', exact: true}).click();
    await left.locator('.tw-range-segment input').nth(3).check();
    await left.getByRole('button', {name: '设为开头', exact: true}).click();
    assert(await page.locator('#tw-range-submit').isDisabled());
    assert.match(await left.locator(':scope > .tw-range-error').textContent(), /结尾在开头之前/);
    await left.getByRole('button', {name: '撤销', exact: true}).click();
    assert(await page.locator('#tw-range-submit').isEnabled());
    const editedBounds = await books.locator('.tw-range-bounds').allTextContents();
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/api/text-alignments/start')),
      page.locator('#tw-range-submit').click(),
    ]);
    assert.equal(submissions.length, 1);
    assert.deepEqual(Object.keys(submissions[0].expected_segment_set_ids).sort(), ['pivot', 'target']);
    assert.deepEqual(Object.keys(submissions[0].reviewed_body_ranges).sort(), ['pivot', 'target']);
    await open();
    assert.deepEqual(await books.locator('.tw-range-bounds').allTextContents(), editedBounds);
    await page.getByText('浏览器测试：模拟提交失败，未执行对齐', {exact: true}).waitFor({state: 'hidden'});
    await page.screenshot({path: path.join(output, 'range-desktop.png')});
    const dimensions = [];
    for (const width of [1280, 768, 414, 375, 320]) {
      await page.setViewportSize({width, height: 800});
      await page.locator('.tw-range-dialog > .tw-dialog-body').evaluate(node => { node.scrollTop = 0; });
      const geometry = await left.evaluate(book => {
        const reading = book.querySelector('.tw-range-reading');
        reading.scrollTop = reading.scrollHeight;
        const rect = selector => {
          const r = book.querySelector(selector).getBoundingClientRect();
          return {top: r.top, bottom: r.bottom, left: r.left, right: r.right};
        };
        return {width: innerWidth, bounds: rect('.tw-range-bounds'), setter: rect('.tw-range-set'),
          footerTop: document.querySelector('.tw-dialog-foot').getBoundingClientRect().top,
          readingHeight: reading.clientHeight,
          overflow: document.documentElement.scrollWidth > innerWidth,
          horizontal: book.scrollWidth > book.clientWidth};
      });
      assert(!geometry.overflow && !geometry.horizontal, JSON.stringify(geometry));
      assert(geometry.readingHeight >= 100, JSON.stringify(geometry));
      assert(geometry.bounds.top >= 0 && geometry.setter.bottom <= geometry.footerTop, JSON.stringify(geometry));
      dimensions.push(geometry);
      await page.screenshot({path: path.join(output, `range-${width}.png`)});
      if (width < 860) {
        await right.scrollIntoViewIfNeeded();
        const visible = await right.evaluate(book => {
          const bounds = book.querySelector('.tw-range-bounds').getBoundingClientRect();
          const setter = book.querySelector('.tw-range-set').getBoundingClientRect();
          const body = book.closest('.tw-dialog-body').getBoundingClientRect();
          return bounds.top >= body.top && setter.bottom <= body.bottom;
        });
        assert(visible, `第二本书的范围和操作按钮在 ${width}px 下应完整可见`);
      }
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser-results.json'), JSON.stringify({dimensions, submissions, errors}, null, 2));
    console.log('正文范围浏览器回归通过（模型与提交响应为模拟）');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
