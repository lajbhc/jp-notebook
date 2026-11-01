/**
 * 手机上传页的 JS 用 Node 跑一遍（不需要浏览器）。
 *
 * 为什么要有这个脚本：页面脚本只在手机上跑，Python 的验证脚本看不到它。
 * 21:10 那次事故就是 `var status = document.getElementById('status')`
 * 撞上了 window.status（浏览器把它当字符串），于是 status.style 是 undefined，
 * 一进 pick() 就抛异常、上传请求根本没发出去，而电脑端只能看到"没收到"。
 *
 * 用法（项目根目录）：
 *   .\venv\Scripts\python.exe -c "from server.upload_server import UPLOAD_PAGE; open('.tmp/page.html','w',encoding='utf-8').write(UPLOAD_PAGE)"
 *   node scripts\check_upload_page_js.js .tmp\page.html
 */

const fs = require('fs');
const vm = require('vm');

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.log('用法: node scripts/check_upload_page_js.js <page.html>');
  process.exit(2);
}

const html = fs.readFileSync(htmlPath, 'utf8');
const match = html.match(/<script>([\s\S]*)<\/script>/);
if (!match) {
  console.log('  [FAIL] 页面里找不到 <script> 块');
  process.exit(1);
}
const js = match[1];

let checks = 0;
let failures = 0;
function check(name, ok, detail) {
  checks += 1;
  if (ok) {
    console.log('  [OK] ' + name);
  } else {
    failures += 1;
    console.log('  [FAIL] ' + name + (detail === undefined ? '' : ' -> ' + detail));
  }
}

const IDS = ['status', 'preview', 'fileinfo', 'retry', 'camera', 'album'];

function makeElement(id) {
  return {
    id: id,
    style: {},
    className: '',
    textContent: '',
    value: '',
    src: '',
    files: null,
    listeners: {},
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
  };
}

function makeSandbox(options) {
  const els = {};
  IDS.forEach((id) => {
    els[id] = makeElement(id);
  });

  const calls = { fetch: 0, fetchUrl: '', xhrSend: 0, pings: [] };

  class FakeImage {
    set src(value) {
      calls.pings.push(String(value));
    }
    get src() {
      return '';
    }
  }

  class FakeFormData {
    constructor() {
      this.entries = [];
    }
    append(key, value, name) {
      this.entries.push([key, value && value.name, name]);
    }
  }

  const sandbox = {
    console: console,
    document: {
      getElementById(id) {
        return els[id] || null;
      },
    },
    Image: FakeImage,
    FormData: FakeFormData,
    URL: { createObjectURL: () => 'blob:fake' },
    setTimeout: setTimeout,
    Promise: Promise,
    Error: Error,
    Date: Date,
    Math: Math,
    JSON: JSON,
    String: String,
    encodeURIComponent: encodeURIComponent,
    XMLHttpRequest: null,
    fetch: function (url) {
      calls.fetch += 1;
      calls.fetchUrl = url;
      return Promise.resolve({
        status: 200,
        text: () => Promise.resolve(JSON.stringify({ ok: true, message: '已收到' })),
      });
    },
  };

  if (options.xhr === 'throw') {
    sandbox.XMLHttpRequest = class {
      constructor() {
        throw new Error('XHR broken');
      }
    };
  } else {
    sandbox.XMLHttpRequest = class {
      constructor() {
        if (options.xhr !== 'no-upload') {
          this.upload = { onprogress: null };
        }
        this.status = 200;
        this.responseText = JSON.stringify({ ok: true, message: '已收到' });
      }
      open() {}
      send() {
        calls.xhrSend += 1;
        if (options.xhr === 'no-upload') {
          check('xhr.upload 不存在时不再抛异常', true);
        }
        if (this.onload) {
          this.onload();
        }
      }
    };
  }

  sandbox.window = sandbox;
  // 浏览器里 window.status 是字符串：给这里也装一个「写进去就变字符串」的属性，
  // 这样 `var status = document.getElementById('status')` 这种写法会像真机一样炸掉，
  // 而不是在测试里蒙混过关。
  try {
    let statusValue = '';
    Object.defineProperty(sandbox, 'status', {
      get() {
        return statusValue;
      },
      set(value) {
        statusValue = String(value);
      },
      configurable: true,
    });
  } catch (err) {
    /* 老 node 上拿不到就退化成普通属性 */
  }

  const context = vm.createContext(sandbox);
  let thrown = null;
  try {
    vm.runInContext(js, context, { filename: 'upload-page.js' });
  } catch (err) {
    thrown = err;
  }

  return { sandbox, els, calls, thrown };
}

function pickPhoto(page) {
  const file = { name: 'IMG_20261030_143700.jpg', size: 3.9 * 1024 * 1024, type: 'image/jpeg' };
  page.els.camera.files = [file];
  let error = null;
  try {
    page.els.camera.listeners.change.call(page.els.camera);
  } catch (err) {
    error = err;
  }
  return { error, file };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

(async function main() {
  console.log('\n== 1. 静态检查 ==');
  check('脚本被 IIFE 包住（顶层 var 不会撞 window.*）', /\(function\s*\(\)\s*\{/.test(js));
  check('没有 var status = 这种会被 window.status 吃掉的声明', !/var\s+status\s*=/.test(js));
  check('没有裸的 status.style 赋值', !/(^|[^A-Za-z0-9_$.])status\.style/.test(js));
  check('有 /ping 打点', js.indexOf('/ping?e=') >= 0);
  check('保留 fetch 兜底', js.indexOf('function fetchUpload') >= 0);

  console.log('\n== 2. 正常路径：拍照 → 上传 → 显示已收到 ==');
  {
    const page = makeSandbox({ xhr: 'ok' });
    check('脚本能跑起来', page.thrown === null, page.thrown && page.thrown.message);
    const { error } = pickPhoto(page);
    check('选图不抛异常', error === null, error && error.message);
    check('状态区没有抛 "Cannot set properties of undefined"', error === null);
    check('XHR 真的 send 了', page.calls.xhrSend === 1, String(page.calls.xhrSend));
    check('提示条显示已收到', /已收到/.test(page.els.status.textContent), page.els.status.textContent);
    check('提示条是绿色样式', page.els.status.className === 'ok', page.els.status.className);
    check('提示条 display 被设成 block', page.els.status.style.display === 'block',
      String(page.els.status.style.display));
    check('重试按钮隐藏着', page.els.retry.style.display === 'none',
      String(page.els.retry.style.display));
    check('打点里有 page-load', page.calls.pings.some((u) => u.indexOf('page-load') >= 0));
    check('打点里有 pick', page.calls.pings.some((u) => u.indexOf('pick') >= 0));
    check('打点里有 upload-done 200', page.calls.pings.some((u) => u.indexOf('upload-done%20200') >= 0
      || u.indexOf('upload-done 200') >= 0));
  }

  console.log('\n== 3. 残血 XHR（upload 为 undefined，安卓 WebView 常见） ==');
  {
    const page = makeSandbox({ xhr: 'no-upload' });
    const { error } = pickPhoto(page);
    check('不抛异常', error === null, error && error.message);
    check('仍然 send 了', page.calls.xhrSend === 1, String(page.calls.xhrSend));
    check('提示条显示已收到', /已收到/.test(page.els.status.textContent), page.els.status.textContent);
  }

  console.log('\n== 4. XHR 直接构造失败 → 退回 fetch ==');
  {
    const page = makeSandbox({ xhr: 'throw' });
    const { error } = pickPhoto(page);
    check('不抛异常', error === null, error && error.message);
    await tick();
    await tick();
    check('记录下 xhr-broken', page.calls.pings.some((u) => u.indexOf('xhr-broken') >= 0));
    check('fetch 被调用了一次', page.calls.fetch === 1, String(page.calls.fetch));
    check('fetch 打的是 /upload', page.calls.fetchUrl === '/upload', page.calls.fetchUrl);
    check('提示条显示已收到', /已收到/.test(page.els.status.textContent), page.els.status.textContent);
  }

  console.log('\n== 5. 选中空文件（安卓相机偶尔这样） ==');
  {
    const page = makeSandbox({ xhr: 'ok' });
    page.els.camera.files = [];
    let error = null;
    try {
      page.els.camera.listeners.change.call(page.els.camera);
    } catch (err) {
      error = err;
    }
    check('不抛异常', error === null, error && error.message);
    check('给出红色提示', page.els.status.className === 'err', page.els.status.className);
    check('提示里教了下一步', /没读到照片/.test(page.els.status.textContent),
      page.els.status.textContent);
    check('没有乱发请求', page.calls.xhrSend === 0, String(page.calls.xhrSend));
  }

  console.log('\n== 6. 点「重新上传这张」 ==');
  {
    const page = makeSandbox({ xhr: 'ok' });
    pickPhoto(page);
    const before = page.calls.xhrSend;
    page.els.retry.listeners.click.call(page.els.retry);
    check('重试又发了一次', page.calls.xhrSend === before + 1, String(page.calls.xhrSend + '/' + before));
  }

  console.log(`\n===== 通过 ${checks - failures} 项，失败 ${failures} 项 =====`);
  if (failures > 0) {
    console.log('上传页 JS 验收未通过。');
    process.exit(1);
  }
  console.log('上传页 JS 验收全部通过。');
})();