// Loads index.html's script with a tiny DOM and checks the favorites list.
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const match = html.match(/<script>([\s\S]*)<\/script>\s*<\/body>/);
if (!match) {
  console.error('script not found');
  process.exit(1);
}

const store = new Map();
global.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
global.sessionStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const els = new Map();
function makeEl(id) {
  const node = {
    id,
    value: id === 'sortOrder' ? 'newest' : '',
    textContent: '',
    innerHTML: '',
    src: '',
    currentSrc: '',
    style: { setProperty() {} },
    dataset: {},
    className: '',
    offsetHeight: 56,
    paused: false,
    readyState: 0,
    videoWidth: 0,
    videoHeight: 0,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) this._s.has(c) ? this._s.delete(c) : this._s.add(c); else if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    removeEventListener() {},
    removeAttribute() {},
    setAttribute() {},
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild() {},
    insertAdjacentHTML() {},
    closest: () => null,
    play: () => Promise.resolve(),
    pause() {},
    load() {},
    focus() {},
  };
  return node;
}
function byId(id) {
  if (!els.has(id)) els.set(id, makeEl(id));
  return els.get(id);
}

global.document = {
  getElementById: byId,
  querySelector: (sel) => byId(sel),
  querySelectorAll: () => [],
  createElement() {
    const node = makeEl('el');
    let text = '';
    Object.defineProperty(node, 'textContent', {
      get() { return text; },
      set(v) {
        text = v == null ? '' : String(v);
        node.innerHTML = text
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
      },
    });
    return node;
  },
  addEventListener() {},
  documentElement: makeEl('html'),
  body: makeEl('body'),
  hidden: false,
  title: '',
};
global.window = global;
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.location = {
  search: '?favorites=1',
  pathname: '/index.html',
  hash: '',
  href: 'http://localhost/index.html?favorites=1',
  origin: 'http://localhost',
};
global.history = { replaceState() {}, pushState() {}, back() {} };
global.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
global.IntersectionObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
global.HTMLMediaElement = { HAVE_CURRENT_DATA: 2, HAVE_ENOUGH_DATA: 4 };
global.alert = () => {};
global.fetch = async (url) => ({
  ok: true,
  json: async () => ({ total: 0, months: [], videos: [], vthumb: true, ffmpeg: false }),
  text: async () => '',
  url,
});

const saved = {
  'output/video/clip-a.mp4': {
    name: 'clip-a.mp4',
    path: 'output/video/clip-a.mp4',
    size: 10,
    modified: 1700000000,
    duration: 3,
  },
};
localStorage.setItem('comfyVideoFavorites', JSON.stringify(saved));

let failures;
try {
  failures = await eval(match[1] + `
;(async () => {
  await new Promise((r) => setTimeout(r, 30));
  const failures = [];
  const grid = document.getElementById('videoGrid');
  const stats = document.getElementById('stats');
  if (!grid.innerHTML.includes('clip-a.mp4')) failures.push('favorite missing from grid: ' + grid.innerHTML.slice(0, 240));
  if (!grid.innerHTML.includes('♥')) failures.push('saved favorite should show a filled heart');
  if (!stats.textContent.includes('1 favorite')) failures.push('stats: ' + stats.textContent);
  if (!document.body.classList.contains('favorites-mode')) failures.push('favorites mode class missing');
  if (!document.getElementById('favoritesNav').classList.contains('active')) failures.push('Favorites tab not active');
  if (document.getElementById('videosNav').classList.contains('active')) failures.push('Videos tab should not be active');

  const video = listFavorites()[0];
  toggleFavorite(video);
  const after = document.getElementById('videoGrid').innerHTML;
  if (!after.includes('No favorites yet')) failures.push('unfavorite should empty the tab');
  if (localStorage.getItem('comfyVideoFavorites') !== '{}') failures.push('store not cleared: ' + localStorage.getItem('comfyVideoFavorites'));

  const again = {
    name: 'clip-b.mp4',
    path: 'output/video/clip-b.mp4',
    size: 4,
    date: new Date(1700000000 * 1000),
    duration: 1,
    url: 'http://localhost/output/video/clip-b.mp4',
  };
  toggleFavorite(again);
  if (!isFavorite(again.path)) failures.push('toggle on failed');
  document.getElementById('searchInput').value = 'nope';
  renderFavorites();
  if (!document.getElementById('videoGrid').innerHTML.includes('No matching favorites')) {
    failures.push('search should hide non-matching favorites');
  }
  document.getElementById('searchInput').value = '';
  renderFavorites();
  if (!document.getElementById('videoGrid').innerHTML.includes('clip-b.mp4')) failures.push('cleared search should show the favorite');
  modalPlaylist = [again];
  currentVideoIndex = 0;
  updateFavoriteChrome(again.path);
  const favBtn = document.getElementById('modalFavBtn');
  if (favBtn.textContent !== '♥') failures.push('player heart should be filled, got ' + favBtn.textContent);
  if (!favBtn.classList.contains('is-on')) failures.push('player heart missing is-on');

  const ordered = promptFieldsFromWorkflow({
    fields: [
      { section: 'prompts', role: 'positive', label: 'Video positive', value: 'a moving cat' },
      { section: 'widgets', role: 'seed', label: 'KSampler · seed', value: 7 },
      { section: 'prompts', role: 'negative', label: 'Image negative', value: 'blur' },
      { section: 'prompts', role: 'positive', label: 'Image positive', value: 'a still cat' },
      { section: 'prompts', role: 'negative', label: 'Video negative', value: 'jitter' },
    ],
  }).map(field => field.label);
  const want = ['Image positive', 'Image negative', 'Video positive', 'Video negative'];
  if (ordered.join('|') !== want.join('|')) failures.push('prompt order ' + ordered.join('|'));
  renderVideoInfo({
    name: 'clip-b.mp4',
    path: 'output/video/clip-b.mp4',
    size: 4,
    date: new Date(1700000000 * 1000),
  }, {
    fields: [
      { section: 'prompts', role: 'positive', label: 'Image positive', value: 'a still cat' },
      { section: 'prompts', role: 'positive', label: 'Video positive', value: 'a moving cat' },
    ],
  });
  const infoHtml = document.getElementById('videoInfoPanel').innerHTML;
  if (!infoHtml.includes('a still cat') || !infoHtml.includes('a moving cat')) failures.push('info panel missing prompts');
  if (infoHtml.includes('KSampler')) failures.push('info panel included a non-prompt field');
  if (!document.getElementById('videoGrid').innerHTML.includes('♡') && !document.getElementById('videoGrid').innerHTML.includes('♥')) {
    failures.push('card heart missing');
  }
  return failures;
})()
`);
} catch (err) {
  console.error('script threw', err);
  process.exit(1);
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log('favorites ui ok');
