/* Витрина магазина внутри Telegram.

   Ванильный JS без сборщика — намеренно: магазин на сотню позиций не нуждается
   во фреймворке, а файл, который можно открыть и прочитать целиком, переживёт
   любую смену моды на инструменты.

   Каталог приезжает одним запросом и фильтруется здесь же, на устройстве:
   переключение категории должно быть мгновенным, а не походом в сеть. Заказ,
   наоборот, всегда уходит на сервер — только он знает настоящие остатки.
*/

const tg = window.Telegram ? window.Telegram.WebApp : null;
const API = new URL('../api/', location.href);
const PHOTOS = new URL('../photos/', location.href);
const CART_KEY = 'mco-shop-cart';
// Личка владельца магазина: вопрос уходит ему самому, а не через бота.
const SELLER = 'seller_username';

const state = {
  shop: 'Магазин',
  currency: '₽',
  deliveryOptions: [],
  catalog: { categories: [], brands: [], sizes: [], products: [] },
  filters: { category: '', brands: new Set(), sizes: new Set(), condition: '', priceMin: 0, priceMax: 0, query: '' },
  priceCeiling: 0,
  cart: [],          // [{ variantId, productId, size, quantity }]
  screen: 'catalog',
  product: null,
  chosenSize: null,
  music: null,      // плейлист из каталога: витрина может играть вчерашний
  loaded: false,    // приехал ли каталог: пока нет — крутится табло загрузки
};

const $ = (id) => document.getElementById(id);
const money = (value) => `${Number(value).toLocaleString('ru-RU')} ${state.currency}`;
const escapeHtml = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

// --- запуск ------------------------------------------------------------------

async function start() {
  if (tg) {
    tg.ready();
    tg.expand();
    // Окно мессенджера вокруг витрины красим в её же чёрный: до включения
    // экран чёрный весь, а окно в это время ещё растёт до полного экрана,
    // и снизу видно его фон — у покупателя с тёмной темой это была чёрная
    // полоса под недорисованной страницей. Побелеет вместе с витриной.
    paintWindow('#000000');
    // Телефон и всё остальное: safe area с высотой чёлки бывает только
    // на телефоне, на компьютере Telegram присылает туда высоту своей шапки.
    document.body.classList.toggle('desktop', !['android', 'ios'].includes(tg.platform));
    // Один expand() Telegram иногда отыгрывает назад: окно приезжает
    // полуоткрытым, сверху виден чат. Поэтому раскрываем ещё и на каждое
    // изменение размера, а вертикальные свайпы отключаем — иначе прокрутка
    // витрины пальцем утягивает окно вниз вместо списка вещей.
    tg.onEvent('viewportChanged', () => { if (!tg.isExpanded) tg.expand(); });
    if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
    // Полный экран (Bot API 8.0): Telegram убирает свою шапку, и витрина
    // занимает экран целиком, как магазин, а не как вкладка в мессенджере.
    // На старом клиенте метод есть, но кидает WebAppMethodUnsupported — а это
    // середина `start()`, и дальше не выполнялось уже ничего: ни каталог,
    // ни музыка, ни заставка. Так витрина и стояла белым экраном в обычном
    // браузере, где telegram-web-app.js всегда объявляет версию 6.0.
    try { tg.requestFullscreen(); } catch { /* старый клиент: окно с шапкой */ }
    tg.onEvent('fullscreenChanged', syncButtons);
    tg.BackButton.onClick(goBack);
    tg.MainButton.onClick(onMainButton);
    // Витрина чёрно-белая: кнопка не берёт цвет ни у темы мессенджера, ни у вещи.
    if (tg.MainButton.setParams) tg.MainButton.setParams({ color: '#0a0a0a', text_color: '#ffffff' });
  }
  loadCart();
  bindEvents();
  lightUp();
  spinLand(document.querySelector('.logo .globe'));
  applyMusic(lastPlaylist());
  rollBrands();
  setSound(soundWanted());
  await loadCatalog();
}

// Цвет окна мессенджера вокруг витрины: полоска под ней и подложка, из-под
// которой окно вырастает до полного экрана. Оба метода на старых клиентах
// сами предупреждают и ничего не делают.
function paintWindow(color) {
  if (!tg) return;
  tg.setBackgroundColor(color);
  tg.setBottomBarColor(color);
}

// Включение кинескопа: экран до этого чёрный и пустой, кадр раскрывается
// из середины наружу (`crt-on` в styles.css), и в конце включения витрина
// под заставкой перестаёт быть запрещённой к отрисовке, а окно мессенджера
// белеет вместе с ней. Раскрывать нечего, если анимации в системе выключены, —
// тогда витрина просто есть, с первого кадра.
function lightUp() {
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  setTimeout(() => {
    document.body.classList.add('lit');
    paintWindow('#ffffff');
  }, still ? 0 : BOOT.on);
}

async function fetchCatalog() {
  // Через VPN с рваным туннелем запрос обрывается молча: соединение не падает,
  // а просто перестаёт отвечать, и витрина висит на заставке до бесконечности.
  // Поэтому свой срок ожидания и одна повторная попытка — второй заход обычно
  // проходит. AbortController, а не AbortSignal.timeout: тот моложе, а витрина
  // открывается и на старых телефонах.
  for (let attempt = 0; ; attempt++) {
    const stop = new AbortController();
    const timer = setTimeout(() => stop.abort(), 8000);
    try {
      return await fetch(new URL('catalog', API), { headers: authHeaders(), signal: stop.signal });
    } catch (error) {
      if (attempt) throw error;
    } finally {
      clearTimeout(timer);
    }
  }
}

async function loadCatalog() {
  try {
    const response = await fetchCatalog();
    if (!response.ok) throw new Error(`каталог не ответил: ${response.status}`);
    const data = await response.json();

    state.shop = data.shop_name || state.shop;
    state.currency = data.currency || state.currency;
    state.deliveryOptions = data.delivery_options || [];
    state.catalog = data.catalog;
    applyMusic(data.music);

    const prices = state.catalog.products.map((p) => p.price);
    state.priceCeiling = prices.length ? Math.ceil(Math.max(...prices) / 1000) * 1000 : 0;

    $('shop-name').textContent = state.shop;
    document.title = state.shop;

    dropSoldFromCart();
    renderCategories();
    renderFilterOptions();
    renderGrid();
    // Знаки марок тянем сразу, не дожидаясь открытия карточки: см. `markFor`.
    state.catalog.brands.forEach(markFor);
    updateCartBadge();
    syncButtons();

    // Ссылка вида /app/#p=12 открывает сразу карточку товара: удобно скинуть
    // покупателю конкретную вещь и проверять этот экран, не листая витрину.
    const deepLink = location.hash.match(/p=(\d+)/);
    if (deepLink) openProduct(Number(deepLink[1]));
  } catch (error) {
    $('empty').hidden = false;
    $('empty').textContent = 'Витрина не загрузилась. Закрой и открой магазин ещё раз.';
    console.error(error);
  } finally {
    state.loaded = true;
  }
}

function authHeaders() {
  // Подпись Telegram: по ней сервер узнаёт, кто пришёл, и что данные не подделаны.
  return tg && tg.initData ? { 'X-Telegram-Init-Data': tg.initData } : {};
}

// --- каталог -----------------------------------------------------------------

function visibleProducts() {
  const { category, brands, sizes, condition, priceMin, priceMax, query } = state.filters;
  const needle = query.trim().toLowerCase();
  return state.catalog.products.filter((product) => {
    if (category && product.category !== category) return false;
    if (brands.size && !brands.has(product.brand)) return false;
    if (condition && product.condition !== condition) return false;
    if (product.price < priceMin) return false;
    if (priceMax && product.price > priceMax) return false;
    if (sizes.size && !product.sizes.some((s) => sizes.has(s.size))) return false;
    if (needle) {
      const haystack = `${product.brand} ${product.name} ${product.color} ${product.category_name}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

function renderCategories() {
  const nav = $('categories');
  const all = state.catalog.products.length;
  const buttons = [`<button class="chip ${state.filters.category ? '' : 'active'}" data-category="">Всё<span class="count">${all}</span></button>`];
  for (const category of state.catalog.categories) {
    const active = state.filters.category === category.slug ? 'active' : '';
    buttons.push(
      `<button class="chip ${active}" data-category="${escapeHtml(category.slug)}">${escapeHtml(category.name)}<span class="count">${category.count}</span></button>`
    );
  }
  nav.innerHTML = buttons.join('');
  nav.querySelectorAll('[data-category]').forEach((button) => {
    button.onclick = () => pickCategory(button.dataset.category);
  });
}

function pickCategory(slug) {
  clearTimeout(turn.timer);
  turn.at = null;
  state.filters.category = slug;
  renderCategories();
  renderGrid();
  haptic('light');
  // Выбранный раздел может лежать за краем ленты — свайпом на него попадают,
  // не видя его, поэтому лента сама подвозит его в середину.
  const chip = $('categories').querySelector('.chip.active');
  if (chip) chip.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
}

// Разделы идут по кругу, как параллель на глобусе: с «Всё» вправо — сразу
// на последний, с последнего влево — снова на «Всё».
//
// И меняются они тоже как на глобусе: планета из шапки доворачивается в ту
// сторону, куда провели пальцем, и на это время отъезжает вглубь вместе
// с каталогом — раздел не подменяется на месте, а оказывается за поворотом
// шара. Список сменяется на полпути, когда его не видно, и возвращается уже
// другим. Планета для этого берётся та самая, что стоит в шапке (`.globe`),
// а не своя копия: другой планеты у магазина нет.
const TURN_SPAN = 520;   // весь переход; смена раздела — на его середине

// Раздел встаёт на полпути перехода, и до тех пор в `state` лежит ещё прошлый.
// Второй свайп, пришедший раньше, отсчитывал шаг от него и приезжал туда же,
// куда первый: два быстрых движения подряд меняли раздел один раз, и второе
// выглядело как несработавший свайп. Поэтому шаг считается от назначенного
// раздела, а не от показанного.
const turn = { timer: 0, at: null };

function spinCategory(step) {
  const slugs = ['', ...state.catalog.categories.map((item) => item.slug)];
  const at = slugs.indexOf(turn.at === null ? state.filters.category : turn.at);
  const next = slugs[(at + step + slugs.length) % slugs.length];
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    pickCategory(next);   // тихий режим: раздел просто меняется
    return;
  }
  turnGlobe(step);
  const grid = $('grid');
  // Свайп за свайпом: прошлый переход отменяется, иначе его конец перебьёт
  // начало нового и список моргнёт.
  grid.getAnimations().forEach((run) => run.cancel());
  // Уход и возврат — одна анимация с прыжком через середину: на прозрачном
  // кадре список перескакивает на другую сторону, и подмены не видно.
  grid.animate([
    { opacity: 1, transform: 'none' },
    { opacity: 0, transform: `translateX(${-step * 26}px) scale(.97)`, offset: .45 },
    { opacity: 0, transform: `translateX(${step * 26}px) scale(.97)`, offset: .55 },
    { opacity: 1, transform: 'none' },
  ], { duration: TURN_SPAN, easing: 'ease-in-out' });
  clearTimeout(turn.timer);
  turn.at = next;
  turn.timer = setTimeout(() => pickCategory(next), TURN_SPAN * 0.45);
}

// Доворот планеты меряется временем оборота (мс), а не углом: материкам он
// прибавляется к часам в `spinLand()`, меридианам вычитается из
// `animation-delay` — сетка крутится каскадом, карта покадрово, но шар один
// и ехать они обязаны вместе. Полторы секунды оборота — это полсотни
// градусов: раздел за поворотом, а не за кругосветкой.
const spin = { offset: 0, run: 0 };

function turnGlobe(step) {
  const globe = document.querySelector('.logo .globe');
  if (!globe) return;
  // Отъезд и возврат: планета уходит вглубь на середине перехода — там же,
  // где сменяется список. Через Web Animations, а не класс с transition:
  // движение туда-обратно это один кадр в середине, а не два состояния.
  globe.animate([
    { transform: 'translate(-50%, -50%) scale(1)' },
    { transform: 'translate(-50%, -50%) scale(.78)', offset: .45 },
    { transform: 'translate(-50%, -50%) scale(1)' },
  ], { duration: TURN_SPAN, easing: 'ease-in-out' });
  const wires = globe.querySelectorAll('.meridian');
  const from = spin.offset;
  const to = from - step * 1400;   // свайп влево уводит поверхность влево
  const begun = performance.now();
  spin.run = begun;                // быстрый второй свайп отменяет первый доворот
  const frame = (now) => {
    if (spin.run !== begun) return;
    const t = Math.min(1, (now - begun) / TURN_SPAN);
    const ease = t < .5 ? 2 * t * t : 1 - (2 - 2 * t) ** 2 / 2;
    spin.offset = from + (to - from) * ease;
    wires.forEach((line) => {
      // Базовая задержка — из разметки, и берётся до первой же правки:
      // дальше в стиле лежит уже сдвинутая.
      const base = line.dataset.delay
        || (line.dataset.delay = getComputedStyle(line).animationDelay);
      const [draw, turn] = base.split(',');
      line.style.animationDelay = `${draw}, ${parseFloat(turn) - spin.offset / 1000}s`;
    });
    if (t < 1) requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

function photoTag(product, className = '') {
  const stub = `<div class="stub ${className}">${escapeHtml((product.brand || product.name || '?').slice(0, 2).toUpperCase())}</div>`;
  if (!product.photos || !product.photos.length) return stub;
  return `<img class="${className}" src="${new URL(product.photos[0], PHOTOS)}" alt="${escapeHtml(product.name)}" loading="lazy" decoding="async">`;
}

function cardHtml(product) {
  const sizes = product.sizes.filter((s) => s.size !== 'ONE').map((s) => s.size);
  const discount = product.old_price && product.old_price > product.price
    ? `<span class="card-tag">−${Math.round((1 - product.price / product.old_price) * 100)}%</span>` : '';
  return `
      <article class="card" data-id="${product.id}">
        <div class="card-photo">${photoTag(product)}${discount}</div>
        <div class="card-body">
          <div class="card-brand">${escapeHtml(product.brand || product.category_name || '')}</div>
          <div class="card-name">${escapeHtml(product.name)}</div>
          <div>
            <span class="card-price">${money(product.price)}</span>
            ${product.old_price ? `<span class="card-old-price">${money(product.old_price)}</span>` : ''}
          </div>
          ${sizes.length ? `<div class="card-sizes">${sizes.join(' · ')}</div>` : ''}
        </div>
      </article>`;
}

// Карточки собираются один раз на каталог, а фильтр их только прячет и показывает.
// Пересборка разметки выбрасывала вместе с ней и `<img>`, а значит, каждая
// смена раздела заставляла браузер заново разжимать фотографии: из Telegram они
// приходят по паре мегапикселей, а в карточке живут шириной в палец. Оттуда и белый
// провал в списке посреди перехода, и полсекунды, за которые на свайп не рисовалось
// вообще ничего. Спрятанная карточка фото не грузит (`loading="lazy"`), а показанная
// раз уже остаётся разжатой — второй заход в раздел встаёт тем же кадром.
let gridFor = null;   // каталог, по которому собрана сетка

function renderGrid() {
  const products = visibleProducts();
  const grid = $('grid');
  $('found').textContent = products.length ? `${products.length} ${plural(products.length, 'вещь', 'вещи', 'вещей')}` : '';
  $('empty').hidden = products.length > 0;

  if (gridFor !== state.catalog) {
    gridFor = state.catalog;
    grid.innerHTML = state.catalog.products.map(cardHtml).join('');
    grid.querySelectorAll('.card').forEach((card) => {
      card.onclick = () => openProduct(Number(card.dataset.id));
    });
    // Фотографии разделов, куда ещё не заходили, подтягиваются в простое, как
    // и знаки марок: иначе первый заход упирается в белое, потому что снимок
    // начинает грузиться в тот момент, когда карточку уже показали. Каталог
    // магазина — пара десятков вещей, это мегабайты, а не десятки.
    const later = window.requestIdleCallback || ((run) => setTimeout(run, 1500));
    later(() => grid.querySelectorAll('img[loading="lazy"]')
      .forEach((photo) => { photo.loading = 'eager'; }));
  }

  const shown = new Set(products.map((product) => product.id));
  grid.querySelectorAll('.card').forEach((card) => {
    card.hidden = !shown.has(Number(card.dataset.id));
  });
}

function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

// --- фильтры -----------------------------------------------------------------

function renderFilterOptions() {
  $('filter-brands').innerHTML = state.catalog.brands
    .map((brand) => `<button class="chip" data-brand="${escapeHtml(brand)}">${escapeHtml(brand)}</button>`).join('')
    || '<span class="note">Бренды не заполнены</span>';
  $('filter-sizes').innerHTML = state.catalog.sizes
    .map((size) => `<button class="chip" data-size="${escapeHtml(size)}">${escapeHtml(size)}</button>`).join('')
    || '<span class="note">Размеры не заполнены</span>';

  $('filter-price-min').max = $('filter-price-max').max = String(priceTop());
  $('price-max').placeholder = String(priceTop());
  if (!state.filters.priceMax) state.filters.priceMax = state.priceCeiling;
  showPrice();

  $('filter-brands').querySelectorAll('[data-brand]').forEach((button) => {
    button.onclick = () => toggleSetFilter(state.filters.brands, button.dataset.brand, button);
  });
  $('filter-sizes').querySelectorAll('[data-size]').forEach((button) => {
    button.onclick = () => toggleSetFilter(state.filters.sizes, button.dataset.size, button);
  });
  $('filter-condition').querySelectorAll('[data-condition]').forEach((button) => {
    button.classList.toggle('active', button.dataset.condition === state.filters.condition);
    button.onclick = () => {
      state.filters.condition = button.dataset.condition;
      $('filter-condition').querySelectorAll('[data-condition]').forEach((other) => {
        other.classList.toggle('active', other === button);
      });
    };
  });
}

function toggleSetFilter(target, value, button) {
  if (target.has(value)) target.delete(value); else target.add(value);
  button.classList.toggle('active', target.has(value));
  haptic('light');
}

function filtersActive() {
  const { brands, sizes, condition, priceMin, priceMax } = state.filters;
  return brands.size > 0 || sizes.size > 0 || Boolean(condition)
    || priceMin > 0 || (priceMax > 0 && priceMax < state.priceCeiling);
}

function resetFilters() {
  state.filters.brands.clear();
  state.filters.sizes.clear();
  state.filters.condition = '';
  state.filters.priceMin = 0;
  state.filters.priceMax = state.priceCeiling;
  renderFilterOptions();
}

// Верх шкалы: самая дорогая вещь каталога, а до его приезда — круглая сотня
// тысяч, чтобы ползунок не стоял в нуле на пустой витрине.
function priceTop() {
  return state.priceCeiling || 100000;
}

// Цена задаётся с обеих сторон, и границы не проходят друг сквозь друга:
// подвинутая толкает вторую перед собой. Иначе «от» уезжает выше «до»,
// и витрина молча пустеет — покупатель видит ошибку, а не свой выбор.
function setPrice(edge, value) {
  const rub = Math.min(Math.max(Math.round(Number(value) || 0), 0), priceTop());
  const filters = state.filters;
  if (edge === 'min') {
    filters.priceMin = rub;
    if (filters.priceMax < rub) filters.priceMax = rub;
  } else {
    // Пустое «до» — это «без потолка», а не «дешевле нуля»: стёртое окошко
    // должно снимать ограничение, а не прятать весь каталог.
    filters.priceMax = rub || priceTop();
    if (filters.priceMin > filters.priceMax) filters.priceMin = filters.priceMax;
  }
  showPrice();
}

// Одно значение на два поля: ползунок и окошко показывают одно и то же,
// с какой бы стороны его ни поменяли.
function showPrice() {
  const { priceMin, priceMax } = state.filters;
  $('filter-price-min').value = String(priceMin);
  $('filter-price-max').value = String(priceMax);
  // Пока граница не сдвинута, окошко пустое, а число видно серым placeholder'ом:
  // иначе набранная цена дописывается к стоящему там нулю — «5000» после нуля
  // становится «05000», и покупателю приходится стирать чужую цифру руками.
  $('price-min').value = priceMin ? String(priceMin) : '';
  $('price-max').value = priceMax < priceTop() ? String(priceMax) : '';
}

// --- карточка товара ---------------------------------------------------------

function openProduct(productId) {
  const product = state.catalog.products.find((item) => item.id === productId);
  if (!product) return;
  state.product = product;

  const withSize = product.sizes.filter((s) => s.size !== 'ONE');
  state.chosenSize = withSize.length === 1 || !withSize.length ? product.sizes[0] : null;

  const gallery = $('gallery');
  gallery.innerHTML = product.photos.length
    ? product.photos.map((file) => `<img src="${new URL(file, PHOTOS)}" alt="${escapeHtml(product.name)}">`).join('')
    : `<div class="stub">${escapeHtml((product.brand || product.name).slice(0, 2).toUpperCase())}</div>`;
  renderDots(product.photos.length);
  $('product-brand').textContent = product.brand || product.category_name || '';
  $('product-name').textContent = product.name;
  $('product-price').textContent = money(product.price);
  $('product-old-price').textContent = product.old_price ? money(product.old_price) : '';
  $('product-condition').textContent = product.condition === 'used' ? 'б/у' : 'новое';
  $('product-description').textContent = product.description || '';
  const note = $('product-note');
  note.textContent = product.color ? `Цвет: ${product.color}` : '';
  // Пустая строка цвета остаётся разделом карточки и тянет на себя промежуток,
  // хотя показывать в ней нечего.
  note.hidden = !note.textContent;
  renderMark(product.brand || '');

  renderSizes();
  show('product');
  // Листать с первого снимка, а не с того, на котором вышли: пока карточка
  // спрятана (`hidden`), прокрутке некуда встать — команда уходит в пустоту,
  // и браузер, показав галерею, возвращает её на прежний снимок сам.
  gallery.scrollLeft = 0;
  handFlip = false;
  startFlip(product.photos.length);
}

// Сторона квадрата, к площади которого приводится любой знак: 68 на 68 для
// квадратного, шире и ниже для вытянутого.
const MARK_SIZE = 68;

// Знак марки сбоку от размеров. Движение подбирается по тому, что нарисовано
// в самом знаке: круглый компас Stone Island крутится, профиль Карла
// Лагерфельда поворачивается вокруг своей оси, поло-пони Ralph Lauren идёт
// галопом, обезьяна BAPE кивает, крючок Carhartt качается, как бирка на вещи.
// Марка, которой здесь нет, просто дышит: крутить подряд всё, что нашлось,
// значит превратить карточку в карусель.
const BRAND_MOVES = {
  'stone-island': 'spin',
  'karl-lagerfeld': 'turn',
  'ralph-lauren': 'trot',
  'bape': 'bob',
  'carhartt': 'swing',
  'alpha-industries': 'drift',
  'palm-angels': 'sway',
};

// Знак марки достаётся заранее и остаётся в памяти: пока покупатель листает
// витрину, файлы уже приехали, и карточка открывается сразу со знаком, а не
// дорисовывает его спустя мгновение после всего остального. Ключ — сама марка:
// по ней же карточка знак и спрашивает.
const marks = new Map();

function markFor(brand) {
  if (marks.has(brand)) return marks.get(brand);

  // Имя марки — имя файла: «Stone Island» → `brands/stone-island.svg`. Если
  // такого нет, пробуем два первых слова: в каталоге попадаются вещи, у которых
  // при разборе поста в марку уехало и название («Stone Island TEDDY FLEECE
  // ZIPPED JACKET»), а знак у них всё равно должен найтись.
  const slug = brand.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
  const short = slug.split('-').slice(0, 2).join('-');
  const names = short !== slug ? [slug, short] : [slug];
  const sources = slug ? names.flatMap((name) => [`brands/${name}.svg`, `brands/${name}.png`]) : [];
  const found = new Promise((resolve) => {
    const logo = new Image();
    logo.alt = '';
    logo.className = BRAND_MOVES[slug] || BRAND_MOVES[short] || 'breathe';
    // Ни svg, ни png не нашлось — знака у марки нет, и карточка подпишет её
    // словом. Логотип каждой новой марки владелец кладёт в `webapp/brands/`
    // руками, и до тех пор карточка должна выглядеть законченной, а не дырявой.
    logo.onerror = () => (sources.length ? (logo.src = sources.shift()) : resolve(null));
    logo.onload = () => {
      // Ровняем знаки по площади, а не по высоте: у компаса Stone Island стороны
      // равны, у надписи Ralph Lauren они относятся как 14 к 1, и при одной
      // высоте первый выглядел бы плакатом, а вторая — ниткой.
      const ratio = logo.naturalWidth / logo.naturalHeight || 1;
      logo.style.height = `${Math.round(MARK_SIZE / Math.sqrt(ratio))}px`;
      resolve(logo);
    };
    logo.onerror();
  });
  marks.set(brand, found);
  return found;
}

function renderMark(brand) {
  const mark = $('product-mark');
  mark.hidden = !brand;
  mark.innerHTML = '';
  mark.dataset.brand = brand;
  if (!brand) return;
  markFor(brand).then((logo) => {
    // Пока знак ехал, покупатель открыл другую вещь — этот знак уже не её.
    if (mark.dataset.brand !== brand) return;
    if (!logo) {
      mark.innerHTML = `<span class="mark-text">${escapeHtml(brand)}</span>`;
      return;
    }
    mark.innerHTML = '';
    mark.append(logo);
    beatMark(logo);
  });
}

// Знак движется в ритме той же песни, что листает снимки, только вдвое
// медленнее: цикл — два такта. Отрицательная задержка сдвигает анимацию в
// текущую точку трека — иначе она стартовала бы в момент открытия карточки,
// то есть в случайной доле, и качалась бы мимо ритма. Длительность цикла
// берём уже посчитанной браузером: у каждого движения свой множитель от
// `--beat`, и повторять его здесь значило бы держать два числа в двух файлах.
function beatMark(logo) {
  logo.parentElement.style.setProperty('--beat', `${Math.round(flipMs * 2)}ms`);
  const full = parseFloat(getComputedStyle(logo).animationDuration) * 1000;
  if (!full) return;
  const pos = musicPos();
  logo.style.animationDelay = `-${Math.round(((pos % full) + full) % full)}ms`;
}

// Снимки листаются сами, в темпе того, что играет: доля и фаза посчитаны
// по самому файлу (гребёнка по спектральному потоку в `src/music.py`), а не
// подобраны на глаз. У каждой песни плейлиста они свои и меняются вместе
// с песней — на альбоме соседние вещи расходятся на десятки ударов.
//
// Пока плейлист не приехал, чисел нет и листать нечего: без музыки
// автолистание не работает вовсе (см. startFlip).
let flipMs = 3000;
let flipOffsetMs = 0;
// Доля удара, а не самого такта: по ней мелькают марки на заставке.
let beatMs = 0;

// Между «пора» и «видно» лежит дорога до экрана: команда браузеру, кадр
// на отрисовку, кадр на композитора, — а звук к тому же уходит в динамик
// не в тот же миг, каким его считает `currentTime`. Дорога одна на всю
// витрину, поэтому и поправка одна: по ней считают момент и снимки, и знак
// марки, и марки на заставке. Больше — витрина спешит, меньше — опаздывает.
const SYNC_MS = 40;

// Где сейчас песня по меркам ритма: время от начала трека, за вычетом фазы
// удара и с поправкой на дорогу до экрана. Дорожка передаётся явно — на
// переходе между песнями звучат обе, а ритм витрина держит по одной из них.
const musicPos = (deck = audio()) => deck.currentTime * 1000 - flipOffsetMs + SYNC_MS;

// Прокрутка снимка — не подмена картинки: браузер везёт его с разгоном
// и торможением, а в конце ещё подтягивает к сетке `scroll-snap`, и всё это
// вместе — около полусекунды. В ритм читается середина пути, а не команда
// на него, поэтому листать начинаем примерно за половину прокрутки. Видно
// опоздание — увеличить, спешку — уменьшить; ничего, кроме момента
// переключения, число не трогает.
const SCROLL_LEAD_MS = 320;
let flipTimer = null;
// Дорожка, чьего начала ждёт листание, — или ничего, когда ждать нечего.
let flipWait = null;
// Галерею листнули пальцем — автолистание отдано человеку насовсем. Это не то
// же самое, что снятый таймер: экран гаснет, таймера нет, а карточка всё ещё
// листается сама, и после включения экрана листание должно вернуться.
let handFlip = false;

// Плейлист магазина: песни лежат в data/, адрес с меткой версии — иначе
// браузер играл бы старую из кэша, имена файлов от плейлиста к плейлисту
// повторяются.
let playlist = [];
let trackIndex = 0;

// Плейлист прошлого открытия. Музыка должна звучать с первого кадра заставки,
// а список приезжает с каталогом — круг до сервера и первые килобайты песни
// съедали секунду, и знак собирался в тишине. Вчерашний список лежит здесь,
// сами песни к этому моменту в кэше браузера (`/music/` отдаётся на неделю),
// поэтому звук идёт сразу. Свежий список ложится сюда же, но играющую песню
// не трогает: присланная владельцем вещь попадёт в витрину со следующего
// открытия — обрывать её на полуслове ради этого незачем.
const MUSIC_KEY = 'mco-shop-playlist';

function lastPlaylist() {
  try { return JSON.parse(localStorage.getItem(MUSIC_KEY)); } catch { return null; }
}

// Играющая дорожка и та, что молчит. Переход — это перекрёстное затухание
// между ними, поэтому «музыка» витрины — не элемент, а вот эта пара.
let live = 0;
const audio = () => $(live ? 'music-b' : 'music');
const idle = () => $(live ? 'music' : 'music-b');

// Сколько длится переход. Темпы у соседних песен разные и сводить их в один
// витрина не пытается — это работа диджея, а не магазина одежды: две секунды
// внахлёст читаются как смена вещи, а не как каша из двух ритмов.
const FADE_MS = 2000;
let fadeTimer = null;

function applyMusic(tracks) {
  // Порядок тасуется один раз за открытие витрины: каталог перечитывается
  // и на возврате из корзины, а перетасовка на живой песне оборвала бы её
  // на полуслове.
  if (!tracks || !tracks.length) return;
  state.music = tracks;
  try { localStorage.setItem(MUSIC_KEY, JSON.stringify(tracks)); } catch { /* приватный режим */ }
  if (playlist.length) return;
  playlist = tracks.slice();
  // Тасовка Фишера — Йетса, а не sort(() => Math.random() - 0.5): вторая
  // на десятке песен заметно любит исходный порядок, и первой каждый раз
  // играла бы почти всегда одна и та же вещь.
  for (let i = playlist.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [playlist[i], playlist[j]] = [playlist[j], playlist[i]];
  }
  // Играть сразу, не дожидаясь конца заставки: марки на ней мелькают в ритм,
  // и музыка нужна именно там. Раньше здесь стоял `false`, а запускало песню
  // первое касание витрины — теперь заставка касаний не пропускает, и играть
  // стало нечему. Telegram своему WebView автозапуск разрешает; браузер на
  // компьютере вправе отказать, на этот случай остаётся `armSound()`.
  playTrack(0, soundWanted());
}

// Ставит песню плейлиста на играющую дорожку и переводит витрину в её темп.
function playTrack(index, play) {
  const track = playlist[trackIndex = index % playlist.length];
  flipMs = track.flip_ms || 3000;
  flipOffsetMs = track.offset_ms || 0;
  beatMs = track.bpm ? 60000 / track.bpm : 0;
  const music = audio();
  music.src = track.url;
  music.volume = 1;
  wire(music);
  if (play) playDeck(music);
  retune();
}

// Всё, чем дорожка живёт: подхват следующей песни.
function wire(music) {
  // Переход начинается за FADE_MS до конца, а не по `ended`: к тому моменту
  // подхватывать уже поздно. Обработчик висит на самой дорожке и молчит,
  // когда она стала фоновой, — иначе уходящая песня звала бы следующую ещё раз.
  music.ontimeupdate = () => {
    if (music !== audio()) return;
    if (!(music.duration > 0)) return;
    // Следующую песню тянем не с первой секунды: её тринадцать мегабайт делят
    // канал с той, что должна зазвучать сейчас, — а нужна она через три минуты.
    if (music.currentTime > 20) preloadNext();
    if (music.duration - music.currentTime <= FADE_MS / 1000) crossfade();
  };
  // Страховка: `duration` бывает неизвестна (поток, битый заголовок), и тогда
  // о конце песни сообщает только он. Встык, зато следующая всё равно играет.
  music.onended = () => { if (music === audio()) playTrack(trackIndex + 1, true); };
  // Вчерашний список мог протухнуть: песню унесли или данные переехали, и метка
  // версии в адресе другая. Узнаём это по тому, что адрес не отдаётся, а в свежем
  // каталоге его нет, — тогда играем по свежему, а не молчим до конца открытия.
  music.onerror = () => {
    if (music !== audio() || !state.music) return;
    if (state.music.some((track) => music.src.endsWith(track.url))) return;
    playlist = [];
    applyMusic(state.music);
  };
}

// Следующая песня подтягивается на молчащую дорожку заранее, пока играет
// текущая: начинать загрузку в момент перехода — значит начинать переход
// с тишины. `preload` в разметке снят, чтобы первая песня не конкурировала
// за канал с фотографиями; здесь он уже нужен — качать нечего, кроме неё.
function preloadNext() {
  const next = playlist[(trackIndex + 1) % playlist.length];
  const deck = idle();
  // Молчащая — значит молчащая: на переходе фоновой числится ещё звучащая
  // песня, и подменить ей src значило бы оборвать её на полуслове.
  // Тот же адрес — уже тянем: присваивать src заново значит начать загрузку
  // сначала, а зовут отсюда с каждым тиком играющей песни.
  if (!next || !deck.paused || deck.src.endsWith(next.url)) return;
  deck.preload = 'auto';
  deck.src = next.url;
}

// Песни сменяют друг друга внахлёст: уходящая затихает, приходящая уже звучит.
// Ритм витрины переключается на новую в самом начале перехода, а не в конце, —
// снимки и знак марки должны качаться в темпе той песни, которую слышно всё
// громче, а не той, что уже уходит.
function crossfade() {
  const out = audio();
  live ^= 1;
  playTrack(trackIndex + 1, true);
  const inn = audio();
  inn.volume = 0;
  const started = performance.now();
  clearInterval(fadeTimer);
  // Громкость шагами по кадру-другому: WebAudio ради двух линейных рамп —
  // это целый граф узлов и своё разрешение на звук в придачу.
  fadeTimer = setInterval(() => {
    const k = Math.min(1, (performance.now() - started) / FADE_MS);
    out.volume = 1 - k;
    inn.volume = k;
    if (k === 1) stopFade();
  }, 40);
}

// Конец перехода — или его отмена, когда музыку выключили на полпути.
// В обоих случаях звучит ровно одна дорожка, во весь голос.
function stopFade() {
  clearInterval(fadeTimer);
  fadeTimer = null;
  idle().pause();
  audio().volume = 1;
  preloadNext();
}

// Витрина живёт в ритме того, что играет сейчас: снимки листаются по долям
// песни, знак марки качается в её же темпе. Со сменой песни оба пересчитываются
// заново — иначе следующая вещь качала бы витрину в темпе предыдущей.
function retune() {
  const logo = $('product-mark').querySelector('img');
  if (logo) beatMark(logo);
  // Только если листание идёт: галерея, которую человек листнул пальцем,
  // остаётся его, и новая песня не отбирает её обратно.
  if (flipTimer) startFlip($('gallery').children.length);
}

// Звук просыпается только после заставки. Пока знак собирается, витрина
// не отвечает ни на что — в том числе не включает музыку от случайного тычка
// по заставке. Дальше пробуем сами, а если WebView отклонил play() (без жеста
// он вправе), разрешением станет первое касание уже собранной витрины.
function armSound() {
  if (!soundWanted()) return;
  setSound(true);
  document.addEventListener('pointerdown', () => {
    if (soundWanted() && audio().paused) setSound(true);
  }, { once: true });
}

// Листание, ждущее, когда песня начнётся: на компьютере она молчит до первого
// щелчка мышью — браузер без него играть не даёт.
const flipWake = () => startFlip($('gallery').children.length);

function stopFlip() {
  clearTimeout(flipTimer);
  flipTimer = null;
  // Ждать песню тоже перестаём: галерею уже листают руками.
  if (flipWait) flipWait.removeEventListener('play', flipWake);
  flipWait = null;
}

// Первое же касание галереи выключает автолистание насовсем: человек начал
// смотреть сам, и вырывать у него снимок из-под пальца нельзя.
function startFlip(count) {
  stopFlip();
  const music = audio();
  if (count < 2) return;
  // Под музыку — значит под музыку: со звуком выключенным темпа нет. Но
  // «молчит сейчас» и «играть не будет» — разное: на компьютере браузер не
  // пускает песню без щелчка мышью, и карточка открывается раньше, чем
  // зазвучало. Тогда листание ждёт начала песни, а не отменяется навсегда, —
  // иначе на компьютере снимки не листались вовсе.
  if (music.paused) {
    if (soundWanted()) { flipWait = music; music.addEventListener('play', flipWake, { once: true }); }
    return;
  }
  const gallery = $('gallery');
  // Лента листается до края и оттуда обратно, а не по кругу. Круг требует
  // прыжка через всю ленту, а на компьютере окно шире снимка, и в конце
  // остаётся не следующий снимок, а полполоски: листание упиралось в неё
  // после первого же шага и дальше стояло на месте.
  let back = false;

  // Каждый шаг считает границу такта заново по часам самого трека. Раньше
  // здесь стоял setInterval: свободный таймер стартовал в случайной точке
  // такта и копил отставание (вкладка в фоне, занятый поток), так что через
  // полминуты снимки листались уже мимо ритма. От `currentTime` уехать некуда.
  const tick = () => {
    const pos = musicPos(music) + SCROLL_LEAD_MS;
    let wait = flipMs - ((pos % flipMs) + flipMs) % flipMs;
    // Граница вплотную к открытию карточки — ждём следующую: снимок, дёрнувшийся
    // в тот же миг, читается как сбой, а не как ритм.
    if (wait < 200) wait += flipMs;
    flipTimer = setTimeout(() => {
      if (state.screen !== 'product' || $('viewer').open) { stopFlip(); return; }
      // Песня встала — экран погас, звук перехватила система, — и это не повод
      // бросать листание насовсем: карточку никто не трогал. Ждём, когда песня
      // пойдёт снова, тем же способом, каким ждём её на компьютере.
      if (music.paused) { startFlip(count); return; }
      const step = gallery.scrollWidth / gallery.children.length;
      const max = gallery.scrollWidth - gallery.clientWidth;
      // Сторона выбирается до шага, а не после: упершись в край и только
      // потом развернувшись, лента пропустила бы такт.
      if (back ? gallery.scrollLeft <= 1 : gallery.scrollLeft >= max - 1) back = !back;
      const at = Math.round(gallery.scrollLeft / step) * step;
      gallery.scrollTo({ left: Math.max(0, Math.min(max, at + (back ? -step : step))), behavior: 'smooth' });
      tick();
    }, wait);
  };
  tick();
}

// Зум щипком. Свой обработчик, а не масштабирование страницы: витрина живёт
// в мини-приложении с `user-scalable=no` — иначе от щипка разъезжается вся
// вёрстка, а не одна картинка. Тянуть увеличенное фото можно пальцем.
const zoom = { img: null, scale: 1, base: 1, spread: 0, x: 0, y: 0, fromX: 0, fromY: 0, cx: 0, cy: 0, px: 0, py: 0 };

// Свайп вверх или вниз закрывает просмотр: в галерее телефона фото закрывают
// так, и палец тянется к жесту раньше, чем к крестику. Крестик остаётся —
// на компьютере и для тех, кто про жест не знает.
const swipe = { x: 0, y: 0, dy: 0, on: false };

function swipeReset() {
  const strip = $('viewer-strip');
  strip.style.transform = '';
  strip.style.opacity = '';
  Object.assign(swipe, { dy: 0, on: false });
}

function spread(touches) {
  return Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
}

function mid(touches, axis) {
  return (touches[0][axis] + touches[1][axis]) / 2;
}

// Щипок увеличивает ту точку, что под пальцами, а не середину снимка:
// разглядывают шов у ворота или бирку в углу, и зум в центр уводил бы их
// за край экрана — приходилось бы догонять пальцем то, на что смотришь.
// Считаем, в какую точку самого снимка ткнули, и держим её под щепотью.
function zoomAnchor(touches) {
  const box = zoom.img.getBoundingClientRect();
  // Середина снимка без сдвига: `transform-origin` у него в центре.
  zoom.cx = box.left + box.width / 2 - zoom.x;
  zoom.cy = box.top + box.height / 2 - zoom.y;
  zoom.px = (mid(touches, 'clientX') - zoom.cx - zoom.x) / zoom.scale;
  zoom.py = (mid(touches, 'clientY') - zoom.cy - zoom.y) / zoom.scale;
}

function zoomApply() {
  zoom.img.style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})`;
  // Пока фото увеличено, соседние снимки в полосе спрятаны: на айфоне от них
  // оставался кусок в углу экрана поверх зума. Полоса при этом не листается
  // сама — жест уже перехвачен в `ontouchmove`. Раньше на время зума полосе
  // меняли `overflow-x`, и вот от этого кусок и оставался: WebKit пересобирает
  // слой прокрутки и не перерисовывает то, что на нём было.
  zoom.img.classList.toggle('zoomed', zoom.scale > 1);
  $('viewer-strip').classList.toggle('zooming', zoom.scale > 1);
}

function zoomReset() {
  if (zoom.img) {
    zoom.img.style.transform = '';
    zoom.img.classList.remove('zoomed');
  }
  Object.assign(zoom, { img: null, scale: 1, base: 1, spread: 0, x: 0, y: 0, px: 0, py: 0 });
  $('viewer-strip').classList.remove('zooming');
}

// Фото на весь экран: в секонде решает деталь — шов, бирка, потёртость,
// а в карточке снимок помещается целиком и потому мелко.
function openViewer(index) {
  const product = state.product;
  if (!product || !product.photos.length) return;
  const strip = $('viewer-strip');
  strip.innerHTML = product.photos
    .map((file) => `<img src="${new URL(file, PHOTOS)}" alt="${escapeHtml(product.name)}">`)
    .join('');
  $('viewer').showModal();
  // Ширину полосы браузер знает только после показа — до него она нулевая
  // и прокрутка к нужному снимку молча уезжает в начало.
  strip.scrollLeft = strip.clientWidth * index;
  if (tg) tg.MainButton.hide();
  haptic('light');
}

function renderDots(count) {
  const dots = $('gallery-dots');
  dots.innerHTML = count > 1
    ? Array.from({ length: count }, (_, index) => `<span class="dot ${index ? '' : 'active'}"></span>`).join('')
    : '';
}

function updateDots() {
  const gallery = $('gallery');
  const dots = $('gallery-dots').children;
  if (!dots.length) return;
  // Какой снимок ближе к центру экрана — тот и активный.
  const current = Math.round(gallery.scrollLeft / (gallery.scrollWidth / dots.length));
  for (let index = 0; index < dots.length; index += 1) {
    dots[index].classList.toggle('active', index === Math.min(current, dots.length - 1));
  }
}

function renderSizes() {
  const product = state.product;
  const box = $('product-sizes');
  const onlyOneSize = product.sizes.length === 1 && product.sizes[0].size === 'ONE';
  if (onlyOneSize) {
    box.innerHTML = '<span class="note">Один размер, вещь в единственном экземпляре</span>';
    return;
  }
  box.innerHTML = product.sizes.map((variant) => {
    const active = state.chosenSize && state.chosenSize.variant_id === variant.variant_id ? 'active' : '';
    const label = variant.size === 'ONE' ? 'Один размер' : variant.size;
    return `<button class="chip ${active}" data-variant="${variant.variant_id}">${escapeHtml(label)}</button>`;
  }).join('');
  box.querySelectorAll('[data-variant]').forEach((button) => {
    button.onclick = () => {
      state.chosenSize = product.sizes.find((v) => v.variant_id === Number(button.dataset.variant));
      renderSizes();
      syncButtons();
      haptic('light');
    };
  });
}

// --- корзина -----------------------------------------------------------------

function loadCart() {
  try {
    state.cart = JSON.parse(localStorage.getItem(CART_KEY) || '[]');
  } catch (error) {
    state.cart = [];
  }
}

function saveCart() {
  localStorage.setItem(CART_KEY, JSON.stringify(state.cart));
  updateCartBadge();
}

function dropSoldFromCart() {
  // Вещь могли купить, пока корзина лежала в памяти телефона.
  const alive = new Map();
  for (const product of state.catalog.products) {
    for (const variant of product.sizes) alive.set(variant.variant_id, { product, variant });
  }
  const before = state.cart.length;
  state.cart = state.cart.filter((item) => alive.has(item.variantId));
  if (state.cart.length !== before) {
    saveCart();
    toast('Часть вещей из корзины уже разобрали');
  }
}

function cartLines() {
  const lines = [];
  for (const item of state.cart) {
    const product = state.catalog.products.find((p) => p.id === item.productId);
    if (!product) continue;
    const variant = product.sizes.find((v) => v.variant_id === item.variantId);
    if (!variant) continue;
    lines.push({ item, product, variant });
  }
  return lines;
}

function cartTotal() {
  return cartLines().reduce((sum, line) => sum + line.product.price * line.item.quantity, 0);
}

function cartCount() {
  return state.cart.reduce((sum, item) => sum + item.quantity, 0);
}

function updateCartBadge() {
  const count = cartCount();
  const badge = $('cart-count');
  badge.textContent = String(count);
  badge.hidden = count === 0;
}

function addToCart() {
  const product = state.product;
  const variant = state.chosenSize;
  if (!product || !variant) {
    toast('Выбери размер');
    return;
  }
  const existing = state.cart.find((item) => item.variantId === variant.variant_id);
  const inCart = existing ? existing.quantity : 0;
  if (inCart + 1 > variant.available) {
    toast('Больше нет в наличии');
    return;
  }
  if (existing) existing.quantity += 1;
  else state.cart.push({ variantId: variant.variant_id, productId: product.id, size: variant.size, quantity: 1 });
  saveCart();
  haptic('success');
  toast('Добавлено в корзину');
  show('catalog');
}

function renderCart() {
  const lines = cartLines();
  $('cart-empty').hidden = lines.length > 0;
  $('cart-items').innerHTML = lines.map(({ item, product, variant }) => `
    <div class="cart-item">
      ${photoTag(product)}
      <div class="cart-item-body">
        <div class="cart-item-title">${escapeHtml(product.brand)} ${escapeHtml(product.name)}</div>
        <div class="cart-item-meta">
          ${variant.size === 'ONE' ? 'один размер' : `размер ${escapeHtml(variant.size)}`}
          · ${item.quantity} шт · ${money(product.price * item.quantity)}
        </div>
      </div>
      <button class="cart-remove" data-remove="${item.variantId}" aria-label="Убрать">×</button>
    </div>`).join('');
  $('cart-total').textContent = money(cartTotal());

  $('cart-items').querySelectorAll('[data-remove]').forEach((button) => {
    button.onclick = () => {
      state.cart = state.cart.filter((item) => item.variantId !== Number(button.dataset.remove));
      saveCart();
      renderCart();
      syncButtons();
      haptic('light');
    };
  });
}

// --- оформление --------------------------------------------------------------

function renderCheckout() {
  const select = $('delivery');
  if (!select.options.length) {
    select.innerHTML = state.deliveryOptions
      .map((option) => `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`).join('');
  }
  const form = $('checkout-form');
  if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && !form.customer_name.value) {
    form.customer_name.value = tg.initDataUnsafe.user.first_name || '';
  }
}

async function submitOrder() {
  const form = $('checkout-form');
  if (!form.reportValidity()) return;

  const payload = {
    customer_name: form.customer_name.value.trim(),
    phone: form.phone.value.trim(),
    delivery: form.delivery.value,
    address: form.address.value.trim(),
    comment: form.comment.value.trim(),
    items: state.cart.map((item) => ({ variant_id: item.variantId, quantity: item.quantity })),
  };

  setMainButton('Отправляем…', { disabled: true, progress: true });
  try {
    const response = await fetch(new URL('order', API), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      // 409 — пока покупатель заполнял форму, вещь забрали. Каталог перечитываем,
      // чтобы человек сразу увидел, чего не стало, а не гадал.
      toast(data.error || 'Не получилось отправить заявку');
      haptic('error');
      if (response.status === 409) await loadCatalog();
      syncButtons();
      return;
    }

    state.cart = [];
    saveCart();
    haptic('success');
    $('done-title').textContent = `Заявка №${data.order_id} принята`;
    $('done-text').textContent = 'Владелец магазина напишет тебе в Telegram, чтобы подтвердить наличие и договориться об оплате.';
    show('done');
    setTimeout(() => { if (tg) tg.close(); }, 4000);
  } catch (error) {
    console.error(error);
    toast('Нет связи. Попробуй ещё раз');
    syncButtons();
  }
}

// --- экраны и кнопки ---------------------------------------------------------

// История экранов: свайп вправо возвращает на прошлый, влево — на тот,
// с которого вернулись. Своя история, а не browser history: витрина живёт
// внутри мессенджера, адресной строки у неё нет.
const trail = { back: [], forward: [] };

function show(screen, move = 'push') {
  if (move === 'push' && screen !== state.screen) {
    trail.back.push(state.screen);
    trail.forward.length = 0;
  }
  // Заявка отправлена — возвращаться в оформление уже некуда.
  if (screen === 'done') { trail.back.length = 0; trail.forward.length = 0; }
  if (screen !== 'product') stopFlip();
  state.screen = screen;
  for (const name of ['catalog', 'product', 'cart', 'checkout', 'done']) {
    $(`screen-${name}`).hidden = name !== screen;
  }
  // Карточка иногда открывалась прокрученной вниз: высота документа меняется
  // в тот же миг (был длинный каталог — стала короткая карточка), и браузер
  // подрезает прокрутку под новую высоту уже после нашей команды. Поэтому
  // повторяем её следующим кадром и ещё раз, когда доедут снимки.
  window.scrollTo(0, 0);
  requestAnimationFrame(() => window.scrollTo(0, 0));
  setTimeout(() => window.scrollTo(0, 0), 120);
  if (screen === 'cart') renderCart();
  if (screen === 'checkout') renderCheckout();
  syncButtons();
}

function goBack() {
  if ($('viewer').open) { $('viewer').close(); return; }
  // Открытый фильтр — тоже «экран»: назад сначала закрывает его.
  if (!$('filters-sheet').hidden) { $('filters-sheet').hidden = true; return; }
  if (!trail.back.length) {
    if (state.screen !== 'catalog') show('catalog');
    return;
  }
  trail.forward.push(state.screen);
  show(trail.back.pop(), 'move');
}

function goForward() {
  if (!trail.forward.length) return;
  trail.back.push(state.screen);
  show(trail.forward.pop(), 'move');
}

function setMainButton(text, options = {}) {
  if (!tg) return;
  const button = tg.MainButton;
  button.setText(text);
  if (options.disabled) button.disable(); else button.enable();
  if (options.progress) button.showProgress(true); else button.hideProgress();
  button.show();
}

function syncButtons() {
  if (!tg) return;
  // В полноэкранном режиме шапки Telegram нет, и его кнопки висят поверх
  // витрины: класс отдаёт им место сверху (`--safe-top` в styles.css).
  document.body.classList.toggle('fullscreen', Boolean(tg.isFullscreen));
  // Кнопки наверху — только мессенджера: его «✕» закрывает витрину, его же
  // «Назад» уводит из карточки. Своя кнопка возврата стояла рядом со стоковым
  // крестиком и читалась как второй выход из одного экрана.
  const needsBack = state.screen !== 'catalog' && state.screen !== 'done';
  tg.BackButton[needsBack ? 'show' : 'hide']();

  if (state.screen === 'catalog') {
    const count = cartCount();
    if (count) setMainButton(`Корзина · ${count} · ${money(cartTotal())}`);
    else tg.MainButton.hide();
  } else if (state.screen === 'product') {
    const needsSize = state.product && state.product.sizes.length > 1 && !state.chosenSize;
    setMainButton(needsSize ? 'Выбери размер' : 'В корзину', { disabled: needsSize });
  } else if (state.screen === 'cart') {
    if (state.cart.length) setMainButton(`Оформить · ${money(cartTotal())}`);
    else tg.MainButton.hide();
  } else if (state.screen === 'checkout') {
    setMainButton('Отправить заявку');
  } else {
    tg.MainButton.hide();
  }
}

function onMainButton() {
  if (state.screen === 'catalog') show('cart');
  else if (state.screen === 'product') addToCart();
  else if (state.screen === 'cart') show('checkout');
  else if (state.screen === 'checkout') submitOrder();
}

function haptic(kind) {
  if (!tg || !tg.HapticFeedback) return;
  if (kind === 'success' || kind === 'error') tg.HapticFeedback.notificationOccurred(kind);
  else tg.HapticFeedback.impactOccurred(kind);
}

let toastTimer = null;
function toast(text) {
  const box = $('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 2500);
}

// --- звук и вход -------------------------------------------------------------

// Выбор запоминается: кто выключил музыку однажды, тот не хочет слышать её
// и на второй день. localStorage в обёртке — в Telegram на iOS он бывает
// недоступен в приватном режиме и кидает исключение на чтении.
const SOUND_KEY = 'mco-shop-sound';

function soundWanted() {
  try { return localStorage.getItem(SOUND_KEY) !== '0'; } catch { return true; }
}

function setSound(on) {
  const music = audio();
  // play() возвращает обещание и отклоняет его, если браузер счёл касание
  // недостаточным основанием. Молча: витрина без музыки — всё ещё витрина.
  // Играть нечего, пока не приехал плейлист: `play()` на пустой дорожке не
  // просто отказывает — он снимает с неё признак «на паузе» навсегда (нечего
  // грузить, нечего и останавливать). Отсюда и `playDeck` вместо `play()`.
  if (on) playDeck(music);
  else { stopFade(); music.pause(); stopFlip(); }
  $('sound').classList.toggle('off', !on);
  $('sound').setAttribute('aria-pressed', String(on));
  try { localStorage.setItem(SOUND_KEY, on ? '1' : '0'); } catch { /* приватный режим */ }
}

// --- сон и пробуждение -------------------------------------------------------

// Экран гаснет — и до страницы это доходит с опозданием, а то и никак:
// visibilitychange в WebView мессенджера приходит через секунду-другую после
// того, как звук уже забрала система. Поэтому спим и просыпаемся не по
// событию, а по кадрам: пока экран горит, браузер рисует, а погас — не рисует
// вовсе. Пропуск между кадрами больше секунды и значит «экран выключали».
// Часы тут браузерные, поэтому переводом времени в телефоне их не сбить.
let lastFrame = 0;

function watchSleep(now) {
  requestAnimationFrame(watchSleep);
  const gap = now - lastFrame;
  lastFrame = now;
  if (gap > 1000 && gap < now) wokeUp();
}

// Витрина проснулась: экран снова горит. Песня продолжается с того места,
// где её застал погасший экран, — догонять пропущенное витрине незачем,
// а снимки листаются по часам самой песни и вернутся вместе с ней.
function wokeUp() {
  wakeAudio();
  wakeFlip();
}

// Снимает песни с эфира: витрину свернули или экран погас — играть некому.
function hold() {
  audio().pause();
  idle().pause();
}

// Возвращение с погасшего экрана. `play()` сразу после пробуждения WebKit
// вправе и отклонить, пока телефон досыпает, — тогда последним доводом
// остаётся касание витрины: слушатель один и тот же, браузер второй раз
// его не поставит.
function wakeAudio() {
  document.addEventListener('pointerdown', wakeAudio, { once: true });
  if (soundWanted()) playDeck(audio());
}

// Листание после сна: карточку никто не трогал — снимки должны листаться
// дальше, а не стоять до конца просмотра.
function wakeFlip() {
  if (handFlip || state.screen !== 'product' || $('viewer').open) return;
  startFlip($('gallery').children.length);
}

// --- звук --------------------------------------------------------------------

// Единственный вход в `play()` для дорожек витрины.
//
// Web Audio здесь больше нет, и это решение, а не упущение. Песня шла через
// граф ради «двери магазина» на заставке: пока собирался знак, она звучала
// приглушённо, как из-за двери, и дверь открывалась вместе с ним. Но дорожка,
// однажды заведённая в граф, отвязаться от него уже не может, а граф,
// переживший сон системы, на айфоне считает частоту не ту: после включения
// экрана песня шла быстрее и выше тоном, утаскивая за собой листание снимков.
// Чинилось трижды — паузой до пробуждения графа, перемоткой, заменой самого
// элемента, — и каждый раз вылезало иначе, вплоть до полной тишины. Дверь
// того не стоит: без графа песня просто играет, а после сна продолжается
// с того же места.
function playDeck(deck) {
  if (!deck.src) return;
  if (!deck.paused) return;
  deck.play().catch(() => {});
}

// Экран загрузки собирает знак магазина на глазах у покупателя, а не ждёт
// каталог молча. По табло быстро мелькают марки каталога, и из них одна за
// другой выцепляются буквы имени магазина: выцепленная буква застывает на
// месте, а вокруг продолжают мелькать чужие. Когда имя собрано целиком,
// вокруг него прорисовывается планета, и собранный знак уезжает в шапку —
// туда, где он стоит всё время работы витрины. Последнюю букву табло держит
// до конца загрузки: погасить заставку раньше витрины — значит показать
// пустую сетку.
const BOOT = { on: 660, flash: 90, slots: 16, lock: 3, fade: 180,
  turn: 1500, draw: 2400, fly: 1400 };

// Марки мелькают в долю удара той песни, что играет сейчас. Доля берётся
// ближайшая к прежней скорости мелькания (`BOOT.flash`): на 98 ударах это
// седьмая, на 74 — девятая доля, и табло идёт примерно так же быстро,
// как раньше, но попадает в музыку. Момент каждой вспышки считается заново
// по часам трека, как и листание снимков: свободный таймер за десяток секунд
// уезжает от ритма. Музыка молчит или темп не измерен — прежняя ровная сетка.
function flashWait() {
  const music = audio();
  if (!beatMs || music.paused) return BOOT.flash;
  const step = beatMs / Math.max(1, Math.round(beatMs / BOOT.flash));
  const pos = musicPos(music);
  const wait = step - ((pos % step) + step) % step;
  // Доля вплотную — ждём следующую: две марки в одном кадре читаются как сбой.
  return wait < step / 3 ? wait + step : wait;
}

// Материки на планете: берега лежат на шаре точками (`land.js`), и витрина
// каждый кадр поворачивает их вокруг оси — тем же оборотом, каким сжимаются
// меридианы. Проекция считается здесь, а не заранее: у поворота шара нет
// плоского двойника, которым обошлось бы одно преобразование CSS, а рисовать
// материки неподвижными на вращающейся сетке — значит рисовать не шар.
// Точка за горизонтом просто не рисуется: линия обрывается на кадр раньше края,
// и это полградуса — меньше пикселя даже на весь экран.
function spinLand(globe) {
  const land = globe && globe.querySelector('.land');
  if (!land || !window.LAND) return;   // без карты планета просто сетка
  const RAD = Math.PI / 1800;          // данные в десятых долях градуса
  // Долгота с широтой раскладываются один раз: дальше поворот — два умножения.
  // Рядом считается путь пера вдоль берега: по нему карта потом прорисовывается
  // так же, как сетка, — линией, а не появлением целиком.
  let longest = 0;
  const rings = window.LAND.map((ring) => {
    const count = ring.length / 2;
    const points = new Float64Array(count * 3);
    const walk = new Float64Array(count);
    for (let i = 0, j = 0; i < ring.length; i += 2, j += 3) {
      const lon = ring[i] * RAD;
      const lat = ring[i + 1] * RAD;
      const flat = Math.cos(lat);
      points[j] = flat * Math.sin(lon);
      points[j + 1] = Math.sin(lat);
      points[j + 2] = flat * Math.cos(lon);
      if (j) {
        walk[j / 3] = walk[j / 3 - 1] + 44 * Math.hypot(
          points[j] - points[j - 3], points[j + 1] - points[j - 2], points[j + 2] - points[j - 1]);
      }
    }
    longest = Math.max(longest, walk[count - 1]);
    return { points, walk };
  });
  // Оборот — те же десять секунд, что у меридианов в styles.css: сетка и карта
  // должны ехать вместе. Начальный угол выбран так, чтобы заставка открывалась
  // на суше, а не на пустом Тихом океане.
  const TURN = 10000;
  const START = -1;
  // Карта прорисовывается вместе с сеткой и тем же способом — пером вдоль
  // линии. Берег тоньше и мельче каркаса, поэтому перо быстрее: самое длинное
  // побережье укладывается в полторы секунды и заканчивается там же, где
  // замыкается контур шара. Проявлением целиком карта и выглядела приклеенной
  // отдельно от планеты. Отсчёт от появления меридианов: до них материки
  // рисовать не на чем.
  const SPAN = 1500;
  const LATER = 550;
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  // Планета заставки — копия готовой планеты шапки: берега уже лежат в `d`,
  // а пунктир сетки в нуле, потому что нарисованная планета это состояние
  // по умолчанию. Кадр вставки браузер успевает нарисовать раньше, чем заведёт
  // анимацию, и на нём вспыхивала готовая планета — за миг до того, как её
  // начинали рисовать. Поэтому копия стирается руками, а на первом же кадре
  // пунктир возвращается анимации: она в каскаде выше inline-стиля и ведёт его
  // сама. При выключенных анимациях не трогаем ничего — там планета обязана
  // просто стоять нарисованной.
  const wires = still ? [] : globe.querySelectorAll('.wire');
  wires.forEach((line) => { line.style.strokeDashoffset = '300'; });
  if (!still) land.removeAttribute('d');
  let masked = wires.length > 0;
  const begun = performance.now();
  const draw = (now) => {
    // Планету заставки в конце подменяют — считать её материки больше незачем.
    if (!globe.isConnected) return;
    if (masked) {                       // кадр вставки позади, пунктир за анимацией
      masked = false;
      wires.forEach((line) => { line.style.strokeDashoffset = ''; });
    }
    const passed = now - begun;
    const angle = START + (passed + spin.offset) / TURN * Math.PI * 2;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const pen = still ? Infinity : (passed - LATER) * longest / SPAN;
    let d = '';
    for (const { points, walk } of rings) {
      let drawing = false;
      for (let i = 0, j = 0; i < points.length; i += 3, j += 1) {
        if (walk[j] > pen) break;                                   // перо не дошло
        const x = points[i];
        const z = points[i + 2];
        if (z * cos - x * sin <= 0) { drawing = false; continue; }  // за горизонтом
        d += `${drawing ? 'L' : 'M'}${(50 + 44 * (x * cos + z * sin)).toFixed(1)} `
          + `${(50 - 44 * points[i + 1]).toFixed(1)}`;
        drawing = true;
      }
    }
    land.setAttribute('d', d);
    requestAnimationFrame(draw);
  };
  requestAnimationFrame(draw);
}

function rollBrands() {
  const board = $('boot');
  const stage = $('boot-logo');
  const line = $('boot-brand');
  let locked = 0;  // сколько букв имени уже выцеплено и стоит на месте
  let frame = 0;

  // Табло постоянной ширины, имя магазина — всегда в его середине: иначе
  // выцепленные буквы прыгали бы с каждой новой маркой, а они должны стоять.
  // Мелькающая марка набрана серым, выцепленные буквы — чёрным: без этого
  // подмена читается как опечатка в названии, а не как собранная буква.
  const merge = (brand, word) => {
    const size = Math.max(BOOT.slots, brand.length);
    const chars = new Array(size).fill(' ');
    const pad = Math.floor((size - brand.length) / 2);
    for (let i = 0; i < brand.length; i += 1) chars[pad + i] = brand[i];
    const start = Math.floor((size - word.length) / 2);
    const cut = (from, to) => escapeHtml(chars.slice(from, to).join(''));
    return `${cut(0, start)}<b>${escapeHtml(word.slice(0, locked))}</b>${cut(start + locked, size)}`;
  };

  const tick = () => {
    const names = state.catalog.brands;
    const word = state.shop;
    // Пока каталог едет, выцеплять буквы не из чего и незачем: имя магазина
    // приезжает вместе с ним, и до тех пор табло мелькает им самим.
    if (names.length && ++frame % BOOT.lock === 0) {
      const next = locked + (word[locked + 1] === ' ' ? 2 : 1);
      // Последняя буква ждёт каталог: собранное имя — знак того, что витрина
      // готова, и собираться раньше неё оно не должно.
      if (next < word.length || state.loaded) locked = next;
    }
    if (locked >= word.length) {
      // Перечисление останавливается: чужие буквы уходят, остаются выцепленные.
      // Каждая — в своей коробке, по ним потом встают кривые; последняя пустая
      // коробка нужна, чтобы измерить базовую линию строки.
      line.innerHTML = `${[...word]
        .map((ch) => `<i>${ch === ' ' ? '&nbsp;' : escapeHtml(ch)}</i>`)
        .join('')}<i class="base"></i>`;
      // Превращение начинается сразу, без паузы на собранное имя: пауза
      // читается не как «имя собралось», а как «витрина подвисла» — буквы
      // встали гротеском и стоят. Замирание остаётся ровно одно, на переход
      // текста в кривые, и оно вчетверо короче.
      turnToMark();
      return;
    }
    // Длинное название режем: табло должно оставаться в одну строку, иначе
    // выцепленные буквы прыгают по строкам вместе с переносом.
    const brand = (names.length
      ? names[Math.floor(Math.random() * names.length)]
      : word).toUpperCase().slice(0, BOOT.slots + 4);
    line.innerHTML = merge(brand, word);
    setTimeout(tick, flashWait());
  };

  // Превращение букв в знак. Текст подменяется своими же контурами — теми же
  // буквами того же кегля на том же месте, подмену не видно ни одним пикселем, —
  // и дальше контуры точка в точку едут в контуры росчерка. Буквы при этом
  // никуда не деваются и ничем не сменяются: одна форма перетекает в другую.
  // Иначе никак: шрифт в шрифт браузер перетекать не умеет.
  const shape = (letter, k) => letter.from.map((from, c) => {
    const to = letter.to[c];
    let d = '';
    for (let i = 0; i < from.length; i += 2) {
      d += `${i ? 'L' : 'M'}${(from[i] + (to[i] - from[i]) * k).toFixed(4)} `
        + `${(from[i + 1] + (to[i + 1] - from[i + 1]) * k).toFixed(4)}`;
    }
    return `${d}Z`;
  }).join('');

  const turnToMark = () => {
    const mark = window.WORDMARK;
    // Контуры сгенерированы под конкретное имя (`tools/wordmark.py`), а имя
    // приезжает из настроек. Чужому имени превращать нечего: шрифт просто
    // меняется — без фокуса, но и без пустого места.
    if (!mark || mark.word !== state.shop) {
      line.classList.add('script');
      stage.classList.add('named');
      setTimeout(drawGlobe, BOOT.turn);
      return;
    }

    const size = parseFloat(getComputedStyle(line).fontSize);
    const box = stage.getBoundingClientRect();
    const x = line.querySelector('i').getBoundingClientRect().left - box.left;
    const y = line.querySelector('.base').getBoundingClientRect().top - box.top;
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.id = 'boot-mark';
    const group = document.createElementNS(NS, 'g');
    const paths = mark.letters.map((letter) => {
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', shape(letter, 0));
      group.appendChild(path);
      return path;
    });
    svg.appendChild(group);

    // Каждая кривая встаёт на свою букву, а не на общее место строки. Ширины
    // и разрядку браузер считает по оптическому размеру шрифта, а тот зависит
    // от кегля, то есть от экрана: на телефоне буквы стоят чуть иначе, чем
    // в единицах, посчитанных генератором. Одной точкой отсчёта расхождение
    // копится к концу слова — «M co.» разъезжалось на четыре процента, и
    // подмена читалась как «текст сменился на похожий». Поправка меряется
    // на месте и тает вместе с превращением: к росчерку буквы приходят туда,
    // где их поставил генератор.
    const shift = mark.letters.map((letter) => (
      line.children[letter.at].getBoundingClientRect().left - box.left
        - (x + letter.x * size)
    ));

    // Кегль собранного знака берём через `font-size`: сама переменная отдаётся
    // нерасчитанным `clamp(...)`, из которого пикселей не вычесть.
    svg.style.fontSize = 'var(--mark-size)';
    stage.appendChild(svg);
    const full = parseFloat(getComputedStyle(svg).fontSize);
    // Куда знак приедет: середина заставки, считая по чернилам росчерка,
    // а не по коробке строки — у росчерка слева свисает хвост «M».
    const [left, bottom, right, top] = mark.to;
    const toX = box.width / 2 - ((left + right) / 2) * full;
    const toY = box.height / 2 + ((bottom + top) / 2) * full;

    // Кривые лежат в единицах кегля и растут вверх от базовой линии — отсюда
    // отражение по y. В начале они стоят ровно там, где браузер нарисовал
    // текст, поэтому подмену не видно; дальше едут и растут вместе с формой.
    let scale = size;
    const place = (k) => {
      scale = size + (full - size) * k;
      group.setAttribute('transform', `translate(${(x + (toX - x) * k).toFixed(2)} `
        + `${(y + (toY - y) * k).toFixed(2)}) scale(${scale.toFixed(3)} ${-scale.toFixed(3)})`);
    };
    // Поправка задана в экранных пикселях, а внутри группы всё умножается
    // на её масштаб — отсюда деление.
    const nudge = (i, k) => paths[i].setAttribute('transform',
      `translate(${(shift[i] * (1 - k) / scale).toFixed(4)} 0)`);
    place(0);
    mark.letters.forEach((letter, i) => nudge(i, 0));

    // Текст не гаснет разом: кривые лежат на нём, но браузер рисует шрифт чуть
    // жирнее, чем svg ту же форму, — сглаживание. Мгновенная подмена читалась
    // как «буквы стали тоньше», а переход в пятую долю секунды — нет.
    // Превращение при этом стоит на месте: за время перехода форма не должна
    // уехать, иначе из-под кривых проступит вторая надпись.
    line.style.transition = `opacity ${BOOT.fade}ms linear`;
    line.style.opacity = '0';
    setTimeout(() => { line.style.visibility = 'hidden'; }, BOOT.fade);

    // Буквы трогаются не разом, а одна за другой: так видно каждую.
    const stagger = 100;
    const span = BOOT.turn - stagger * (mark.letters.length - 1);
    // Разгон и торможение по четверти хода, между ними — ровное движение.
    // У гладкой ступеньки (smootherstep) середина вдвое быстрее среднего, и на
    // телефоне это читалось так: буква стоит, стоит, а потом дёргается. Здесь
    // скорость меняется только на концах, а форму видно всё время.
    const RAMP = 0.28;
    const ease = (t) => {
      const v = 1 / (1 - RAMP);            // скорость на ровном участке
      if (t < RAMP) return v * t * t / (2 * RAMP);
      if (t > 1 - RAMP) return 1 - v * (1 - t) ** 2 / (2 * RAMP);
      return v * (t - RAMP / 2);
    };
    const begun = performance.now() + BOOT.fade;
    let ended = false;
    // Знак для шапки — те же кривые, что собрались здесь, а не заново набранная
    // надпись: тот же шрифт ложится в шапке иначе (кегль, хинтинг, начертание),
    // и подмена читается как «логотип сменился на другой». Живая надпись
    // остаётся в разметке ради поиска и чтения с экрана, но не рисуется.
    const headMark = () => {
      const head = document.querySelector('.logo');
      const box = head.getBoundingClientRect();
      const globe = head.querySelector('.globe').getBoundingClientRect();
      const kegl = parseFloat(getComputedStyle(head.querySelector('h1')).fontSize);
      const svg = document.createElementNS(NS, 'svg');
      svg.id = 'head-mark';
      const group = document.createElementNS(NS, 'g');
      mark.letters.forEach((letter) => {
        const path = document.createElementNS(NS, 'path');
        path.setAttribute('d', shape(letter, 1));
        group.appendChild(path);
      });
      // Чернила знака — по центру планеты, как и на заставке. Оттого перелёт,
      // считанный по планетам, кладёт один знак на другой без смещения.
      const [left, bottom, right, top] = mark.to;
      const x = globe.left - box.left + globe.width / 2 - ((left + right) / 2) * kegl;
      const y = globe.top - box.top + globe.height / 2 + ((bottom + top) / 2) * kegl;
      group.setAttribute('transform', `translate(${x.toFixed(2)} ${y.toFixed(2)}) `
        + `scale(${kegl} ${-kegl})`);
      svg.appendChild(group);
      head.appendChild(svg);
      head.classList.add('marked');
    };

    const finish = () => {
      if (ended) return;
      ended = true;
      place(1);
      mark.letters.forEach((letter, i) => {
        paths[i].setAttribute('d', shape(letter, 1));
        nudge(i, 1);
      });
      stage.classList.add('named');
      headMark();
      drawGlobe();
    };
    const frame = (now) => {
      if (ended) return;
      const passed = now - begun;
      place(ease(Math.max(0, Math.min(1, passed / BOOT.turn))));
      mark.letters.forEach((letter, i) => {
        const k = Math.max(0, Math.min(1, (passed - i * stagger) / span));
        paths[i].setAttribute('d', shape(letter, ease(k)));
        nudge(i, ease(k));
      });
      if (passed < BOOT.turn) { requestAnimationFrame(frame); return; }
      finish();
    };
    requestAnimationFrame(frame);
    // Кадры приходят не всегда: витрину свернули, экран погас, браузер экономит
    // батарею — и покадровая анимация встаёт. Знак обязан собраться в любом
    // случае, иначе витрина навсегда останется под заставкой.
    setTimeout(finish, BOOT.fade + BOOT.turn + 500);
  };

  // Планета появляется только теперь и рисуется на глазах: заставка берёт копию
  // той самой планеты, что стоит в шапке. Копия, а не она сама, потому что
  // прорисовка начинается в тот момент, когда элемент появляется в разметке,
  // а планета шапки нарисовалась давно. В конце перелёта копия встанет
  // на место оригинала — тогда в шапке окажется ровно то, что собралось
  // по центру экрана.
  const drawGlobe = () => {
    const globe = document.querySelector('.logo .globe').cloneNode(true);
    stage.prepend(globe);
    spinLand(globe);
    setTimeout(flyToHead, BOOT.draw);
  };

  // Знак уезжает не «куда-то в угол», а ровно на своё место в шапке: там та же
  // планета стоит всё время работы витрины, и подмена заставки настоящей шапкой
  // не читается как склейка. Перелёт считается по коробкам, а не по числам:
  // размер планеты в шапке задан вёрсткой и от неё же должен зависеть.
  const flyToHead = () => {
    const head = document.querySelector('.logo');
    const from = stage.getBoundingClientRect();
    const to = head.querySelector('.globe').getBoundingClientRect();
    // Знак шапки на время перелёта спрятан: иначе знаков два — летящий и уже
    // стоящий на месте, и один проступает сквозь другой. Гаснет только фон
    // заставки, сам знак не теряет непрозрачности ни на кадр; в конце знаки
    // меняются местами в одном кадре, а место и кегль у них общие.
    head.style.visibility = 'hidden';
    stage.style.transform = `translate(${to.left + to.width / 2 - from.left - from.width / 2}px, `
      + `${to.top + to.height / 2 - from.top - from.height / 2}px) scale(${to.width / from.width})`;
    stage.classList.remove('named');   // «worldwide» остаётся на заставке
    setTimeout(() => { board.style.backgroundColor = 'transparent'; }, BOOT.fly * 0.6);
    // Прячем совсем, а не только гасим: прозрачный слой поверх витрины
    // продолжал бы существовать, а он на весь экран.
    setTimeout(() => {
      // Планета заставки встаёт на место планеты шапки: не вторая такая же,
      // а тот же узел. Но узел, вынутый из разметки, теряет свои анимации —
      // браузер отменяет их и заводит заново, и планета в шапке пропадала
      // и рисовалась с нуля второй раз. Поэтому время каждой анимации
      // запоминается и возвращается сразу после переезда: прорисовка остаётся
      // законченной, а меридианы не перескакивают.
      const globe = stage.querySelector('.globe');
      const clock = globe.getAnimations
        ? globe.getAnimations({ subtree: true }).map((run) => run.currentTime) : [];
      head.querySelector('.globe').replaceWith(globe);
      if (globe.getAnimations) {
        globe.getAnimations({ subtree: true }).forEach((run, i) => {
          if (clock[i] != null) run.currentTime = clock[i];
        });
      }
      board.hidden = true;
      head.style.visibility = '';
      armSound();          // витрина собралась — теперь её можно и трогать, и слушать
    }, BOOT.fly);
  };

  tick();
}

// --- события -----------------------------------------------------------------

// Свайп по экрану значит разное в каталоге и внутри него. В каталоге он
// крутит разделы по кругу (вправо — предыдущий, влево — следующий): листать
// историю там нечего, а разделы — то, между чем ходят чаще всего. В карточке
// и дальше по заявке он листает историю, как в браузере: вправо — назад,
// влево — вперёд. Горизонтальные ленты (галерея, чипсы, просмотр фото) жест
// не отдают: там свайп листает их содержимое.
// Куда ведёт боковой жест, решает экран, а не устройство: палец, мышь
// и тачпад приходят сюда с одним и тем же сдвигом по горизонтали.
function navSwipe(dx) {
  if (state.screen === 'catalog') {
    spinCategory(dx > 0 ? -1 : 1);
    return;
  }
  haptic('light');
  if (dx > 0) goBack(); else goForward();
}

// Жест наш, только если витрина открыта и его не отдаёт лента под курсором.
// Открытая панель фильтров жеста не берёт вовсе: она листается только вверх
// и вниз, а вбок не двигается никуда — ни разделы под ней не крутит, ни сама
// не закрывается. Раньше свайпом её закрывало, и панель болталась вбок
// от каждого движения пальцем наискось: покупатель ведёт список размеров,
// а из-под него уезжает весь фильтр. Подпись поля (label) — вместе с полем:
// протяжка по ней метит в ручку ползунка, а не в разделы каталога.
function navReady(target) {
  return !$('viewer').open && $('boot').hidden && $('filters-sheet').hidden
    && !target.closest('.gallery, .chips, .viewer-strip, label, input, textarea, select');
}

function bindNavSwipe() {
  const nav = { x: 0, y: 0, on: false, lock: false, decided: false };
  document.addEventListener('touchstart', (event) => {
    nav.lock = false;
    nav.decided = false;
    nav.on = event.touches.length === 1 && navReady(event.target);
    nav.x = event.touches[0].clientX;
    nav.y = event.touches[0].clientY;
  }, { passive: true });
  // Куда жест — решается один раз, на первых пяти пикселях, и больше не
  // пересматривается. Пересматривать нельзя по обеим причинам сразу: браузер
  // отдаёт отмену прокрутки только пока не решил сам (запас у него — десяток
  // пикселей), а живой палец на боковом свайпе всё равно уезжает вниз, и
  // проверка «вбок больше, чем вниз» в конце жеста требовала вести ровно
  // по линейке. Признали боковым — держим прокрутку до конца жеста; признали
  // прокруткой — жест не наш, дальше не мешаем.
  document.addEventListener('touchmove', (event) => {
    if (!nav.on || event.touches.length !== 1) return;
    const dx = event.touches[0].clientX - nav.x;
    const dy = event.touches[0].clientY - nav.y;
    if (!nav.decided) {
      if (Math.abs(dx) < 5 && Math.abs(dy) < 5) return;   // ещё непонятно, куда ведут
      nav.decided = true;
      // Наклон до полусотни градусов считаем боковым: пальцем по стеклу
      // ровной горизонтали не проводит никто.
      nav.lock = Math.abs(dx) > Math.abs(dy) * 0.8;
      nav.on = nav.lock;
      // Признали прокруткой — это самое движение отдаём браузеру нетронутым.
      // Отменённый на нём переход отменяет и прокрутку: судьбу движения
      // браузер решает по первому же событию, которому не дали хода, —
      // и быстрый рывок пальцем вниз повисал на месте.
      if (!nav.lock) return;
    }
    if (event.cancelable) event.preventDefault();
  }, { passive: false });
  document.addEventListener('touchend', (event) => {
    if (!nav.on) return;
    nav.on = false;
    const dx = event.changedTouches[0].clientX - nav.x;
    // Направление уже выбрано на первых пикселях, здесь остаётся длина:
    // короткий сдвиг — это касание с дрожью, а не листание.
    if (!nav.lock || Math.abs(dx) < 45) return;
    navSwipe(dx);
  }, { passive: true });
}

// На компьютере пальца нет, а жест нужен тот же. Мышью свайп — это протяжка
// (нажал, повёл вбок, отпустил), на тачпаде — два пальца вбок, и приезжают
// они колесом. Своими обработчиками, а не общими pointer-событиями: касания
// разобраны выше вместе с прокруткой, наклоном и двумя пальцами, и переписать
// работающий жест ради мыши — поменять целое на часть.
function bindNavMouse() {
  let from = null;
  document.addEventListener('mousedown', (event) => {
    from = event.button === 0 && navReady(event.target) ? event.clientX : null;
  });
  // Картинку протяжка иначе тащит за собой призраком. Гасим сам перенос,
  // а не нажатие: погашенное нажатие не переводит фокус, и клик по подписи
  // поля переставал открывать поле.
  document.addEventListener('dragstart', (event) => { if (from !== null) event.preventDefault(); });
  document.addEventListener('mouseup', (event) => {
    const start = from;
    from = null;
    if (start === null) return;
    const dx = event.clientX - start;
    if (Math.abs(dx) < 60) return;
    // Протяжка через полэкрана выделяет по дороге текст, и выделение
    // остаётся висеть на новом разделе.
    const picked = window.getSelection();
    if (picked) picked.removeAllRanges();
    navSwipe(dx);
  });

  // Тачпад шлёт одно движение пальцев десятком событий, да ещё с инерцией
  // после того, как их подняли. Поэтому копим сумму и срабатываем один раз
  // на порог, а хвост жеста пропускаем: иначе одна протяжка прокрутила бы
  // разделы по кругу.
  let roll = 0;
  let push = 0;
  let spent = false;
  let down = false;
  let quiet = null;
  document.addEventListener('wheel', (event) => {
    if (!navReady(event.target)) return;
    const speed = Math.abs(event.deltaX);
    clearTimeout(quiet);
    quiet = setTimeout(() => { roll = push = 0; spent = down = false; }, 120);
    // Вдоль или поперёк — решается один раз за движение, как и у пальца,
    // и больше не пересматривается. Пересматривать нельзя по двум причинам
    // сразу: прокрутка каталога идёт с дрожью вбок, и эта дрожь, копившаяся
    // по всей длине, сама доходила до порога и меняла раздел; а первое
    // событие прокрутки на маке приезжает вовсе без вертикали — по нему
    // одному любое движение выглядит боковым.
    if (down) return;
    // Направление ждёт первых заметных пикселей, как и у пальца. На маке
    // начало и конец жеста приезжают пустыми событиями (ни вбок, ни вниз),
    // и по такому одному направление решать нельзя: нулевая вертикаль
    // «не меньше» нулевой горизонтали, и весь боковой свайп записывался
    // в прокрутку — до конца жеста, потому что решение не пересматривается.
    if (speed < 2 && Math.abs(event.deltaY) < 2) return;
    if (Math.abs(event.deltaY) >= speed) { down = true; return; }
    // Раздел уже повернули — остаток жеста это инерция, и она затухает.
    // Пошло на разгон — значит пальцы вернулись на стекло, и это уже
    // следующий свайп. По времени такое не отличить: инерция на маке идёт
    // секундами, и по таймеру каждый второй свайп пропадал в ней целиком.
    if (spent) {
      if (speed <= push + 2) { push = speed; return; }
      spent = false;
      roll = 0;
    }
    push = speed;
    roll += event.deltaX;
    if (Math.abs(roll) < 80) return;
    spent = true;
    // Пальцы уезжают влево — содержимое едет вправо, поэтому знак обратный.
    navSwipe(-roll);
    roll = 0;
    // Прокрутке не мешаем ничем: отменять браузеру нечего, и вертикаль
    // остаётся его. Назад по своей истории он этим жестом больше не уходит —
    // это снято `overscroll-behavior-x` в стилях.
  }, { passive: true });
}

function bindEvents() {
  bindNavSwipe();
  bindNavMouse();
  requestAnimationFrame(watchSleep);
  // Выход из витрины не мгновенный: окно ещё едет вниз, а звук уже вернулся
  // мессенджеру — и трек на долю секунды бьёт во весь голос. Глушим сразу,
  // как страница ушла из виду; вернулись — играет дальше с того же места.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      hold();
      stopFlip();
    } else {
      // Кадры о таком возвращении сказали бы и сами, но событие приходит
      // раньше: витрина сворачивалась в чат, а не гасла вместе с экраном.
      wokeUp();
    }
  });
  $('sound').onclick = () => setSound(audio().paused);
  $('cart-button').onclick = () => show('cart');
  // Вопрос продавцу — в личку владельцу магазина: Telegram открывает чат сам,
  // а витрина сворачивается. Своего диалога-подтверждения нет: переход в чат
  // обратим одной кнопкой «назад», спрашивать не о чем.
  $('contact').onclick = () => {
    if (!tg) return toast('Вопрос продавцу — из магазина в Telegram');
    tg.openTelegramLink('https://t.me/' + SELLER);
  };
  $('gallery').onscroll = updateDots;
  $('gallery').onpointerdown = () => { handFlip = true; stopFlip(); };
  $('gallery').onclick = (event) => {
    const photos = [...$('gallery').querySelectorAll('img')];
    const index = photos.indexOf(event.target);
    if (index >= 0) openViewer(index);
  };
  // Тап по увеличенному фото возвращает его в размер, по обычному — закрывает.
  $('viewer').onclick = () => (zoom.scale > 1 ? zoomReset() : $('viewer').close());
  $('viewer-close').onclick = () => $('viewer').close();
  // Кнопку мессенджера прячем на время просмотра и возвращаем после:
  // «В корзину» рисуется поверх картинки и закрывает её нижнюю треть.
  $('viewer').onclose = () => { zoomReset(); swipeReset(); syncButtons(); };

  const strip = $('viewer-strip');
  strip.ontouchstart = (event) => {
    if (event.touches.length === 2) {
      zoom.img = strip.children[Math.round(strip.scrollLeft / strip.clientWidth)];
      zoom.base = zoom.scale;
      zoom.spread = spread(event.touches);
      zoomAnchor(event.touches);
    } else if (zoom.scale > 1) {
      zoom.fromX = event.touches[0].clientX - zoom.x;
      zoom.fromY = event.touches[0].clientY - zoom.y;
    } else {
      Object.assign(swipe, { x: event.touches[0].clientX, y: event.touches[0].clientY, dy: 0, on: true });
    }
  };
  strip.ontouchmove = (event) => {
    if (swipe.on && event.touches.length === 1) {
      swipe.dy = event.touches[0].clientY - swipe.y;
      // Горизонтальный жест отдаём полосе: это листание, а не закрытие.
      if (Math.abs(event.touches[0].clientX - swipe.x) > Math.abs(swipe.dy)) {
        swipeReset();
      } else {
        // Фото едет за пальцем и гаснет: видно, что тянешь к закрытию.
        strip.style.transform = `translateY(${swipe.dy}px)`;
        strip.style.opacity = String(Math.max(0.2, 1 - Math.abs(swipe.dy) / 400));
      }
    }
    if (!zoom.img) return;
    event.preventDefault();
    if (event.touches.length === 2) {
      zoom.scale = Math.min(4, Math.max(1, zoom.base * spread(event.touches) / zoom.spread));
      // Снимок едет так, чтобы взятая точка осталась под пальцами: щепоть
      // при этом можно и двигать — фото поедет за ней, как за одним пальцем.
      zoom.x = mid(event.touches, 'clientX') - zoom.cx - zoom.px * zoom.scale;
      zoom.y = mid(event.touches, 'clientY') - zoom.cy - zoom.py * zoom.scale;
      // В обычном размере снимок стоит ровно посередине, а не там, где щипали.
      if (zoom.scale === 1) { zoom.x = 0; zoom.y = 0; }
    } else if (zoom.scale > 1) {
      zoom.x = event.touches[0].clientX - zoom.fromX;
      zoom.y = event.touches[0].clientY - zoom.fromY;
    }
    zoomApply();
  };
  strip.ontouchend = (event) => {
    // Палец, оставшийся от щипка, продолжает тянуть снимок с того же места:
    // без этого фото прыгало бы на сдвиг, накопленный щипком.
    if (event.touches.length === 1 && zoom.scale > 1) {
      zoom.fromX = event.touches[0].clientX - zoom.x;
      zoom.fromY = event.touches[0].clientY - zoom.y;
    }
    // Порог, чтобы случайный сдвиг пальца при листании не закрывал просмотр.
    const close = swipe.on && Math.abs(swipe.dy) > 90;
    swipeReset();
    if (close) $('viewer').close();
    if (zoom.scale <= 1) zoomReset();
  };

  let searchTimer = null;
  $('search').oninput = (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.query = event.target.value;
      renderGrid();
    }, 150);
  };

  // Цену запоминаем на открытии панели: стрелка над клавиатурой отменяет
  // набранное и возвращает её сюда. В первый раз за сеанс здесь и лежит
  // «от нуля до самой дорогой вещи».
  let priceBefore = null;
  $('filters-button').onclick = () => {
    $('filters-sheet').hidden = false;
    priceBefore = { min: state.filters.priceMin, max: state.filters.priceMax };
  };
  $('filters-close').onclick = () => { $('filters-sheet').hidden = true; };
  $('filters-sheet').onclick = (event) => {
    if (event.target === $('filters-sheet')) $('filters-sheet').hidden = true;
  };
  $('filter-price-min').oninput = (event) => setPrice('min', event.target.value);
  $('filter-price-max').oninput = (event) => setPrice('max', event.target.value);
  // Набранное руками принимаем по окончании ввода, а не на каждой цифре:
  // «12000» на первом же нажатии стало бы ценой в рубль и утащило бы за собой
  // вторую границу.
  $('price-min').onchange = (event) => setPrice('min', event.target.value);
  $('price-max').onchange = (event) => setPrice('max', event.target.value);

  // У цифровой клавиатуры нет своего «готово»: Enter принимает набранное
  // и убирает её, стрелка сверху — убирает, ничего не приняв.
  const kbdHide = $('kbd-hide');
  // Пока набирают цену, окно застывает: `y` — прокрутка витрины, к которой
  // страницу возвращают, `field` — окошко, которое обязано остаться на виду.
  let typing = null;
  // Клавиатура вёрстку не двигает, и окошко цены оказывалось под ней. Поэтому
  // на время набора панель ложится ровно на видимую часть страницы: где эта
  // часть лежит и какой она высоты, visualViewport знает сам (`pageTop`,
  // `height`). Высоту клавиатуры вычитанием из `window.innerHeight` больше
  // не меряем: внутри Telegram окно webview выше экрана, и панель уезжала выше
  // клавиатуры на её же высоту — от фильтров оставалась полоска в две строки,
  // и она же оставалась висеть после того, как клавиатуру убирали.
  const fitKeyboard = () => {
    const sheet = $('filters-sheet');
    const view = window.visualViewport;
    if (!typing || !view) {
      sheet.style.position = sheet.style.top = sheet.style.height = '';
      return;
    }
    // Браузер тянет страницу сам, чтобы показать поле, — а поле и так поднято
    // над клавиатурой, и от его прокрутки остаётся только дёрганый каталог
    // за затемнением. Поэтому витрина стоит там, где стояла.
    window.scrollTo(0, typing.y);
    sheet.style.position = 'absolute';
    sheet.style.top = `${view.pageTop}px`;
    sheet.style.height = `${view.height}px`;
    // Внутри панели прокрутка своя, и цена — последний блок: после подъёма
    // её надо подтянуть к видимой части, иначе она уедет под нижний край.
    typing.field.closest('.filter-block').scrollIntoView({ block: 'nearest' });
  };
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', fitKeyboard);
    window.visualViewport.addEventListener('scroll', fitKeyboard);
  }
  window.addEventListener('scroll', () => { if (typing) window.scrollTo(0, typing.y); });
  ['price-min', 'price-max'].forEach((id) => {
    const input = $(id);
    input.onfocus = () => {
      typing = { y: window.scrollY, field: input };
      kbdHide.hidden = false;
      fitKeyboard();
    };
    // Панель возвращают на место здесь, а не по событию visualViewport:
    // после «убрать клавиатуру» его можно и не дождаться.
    input.onblur = () => { typing = null; kbdHide.hidden = true; fitKeyboard(); };
    input.onkeydown = (event) => { if (event.key === 'Enter') input.blur(); };
  });
  // pointerdown, а не click: клик приходит уже после того, как окошко потеряло
  // фокус и отдало набранное в onchange, — отменять было бы нечего.
  kbdHide.onpointerdown = (event) => {
    event.preventDefault();
    state.filters.priceMin = priceBefore ? priceBefore.min : 0;
    state.filters.priceMax = priceBefore ? priceBefore.max : priceTop();
    showPrice();
    if (document.activeElement) document.activeElement.blur();
    kbdHide.hidden = true;
  };
  $('filters-reset').onclick = () => { resetFilters(); renderGrid(); };
  $('filters-apply').onclick = () => {
    $('filters-sheet').hidden = true;
    $('filters-button').classList.toggle('on', filtersActive());
    renderGrid();
    haptic('light');
  };

  // В браузере (без Telegram) кнопок мессенджера нет — показываем свои,
  // иначе витрину невозможно отладить на компьютере.
  if (!tg) {
    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;display:flex;gap:8px;padding:10px;background:var(--bg);border-top:1px solid var(--line)';
    bar.innerHTML = '<button class="button ghost" id="dev-back">Назад</button><button class="button" id="dev-main">Дальше</button>';
    document.body.appendChild(bar);
    bar.querySelector('#dev-back').onclick = goBack;
    bar.querySelector('#dev-main').onclick = onMainButton;
  }
}

start();
