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

const state = {
  shop: 'Магазин',
  currency: '₽',
  deliveryOptions: [],
  catalog: { categories: [], brands: [], sizes: [], products: [] },
  filters: { category: '', brands: new Set(), sizes: new Set(), condition: '', priceMax: 0, query: '' },
  priceCeiling: 0,
  cart: [],          // [{ variantId, productId, size, quantity }]
  screen: 'catalog',
  product: null,
  chosenSize: null,
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
    tg.BackButton.onClick(goBack);
    tg.MainButton.onClick(onMainButton);
  }
  loadCart();
  bindEvents();
  await loadCatalog();
}

async function loadCatalog() {
  try {
    const response = await fetch(new URL('catalog', API), { headers: authHeaders() });
    if (!response.ok) throw new Error(`каталог не ответил: ${response.status}`);
    const data = await response.json();

    state.shop = data.shop_name || state.shop;
    state.currency = data.currency || state.currency;
    state.deliveryOptions = data.delivery_options || [];
    state.catalog = data.catalog;

    const prices = state.catalog.products.map((p) => p.price);
    state.priceCeiling = prices.length ? Math.ceil(Math.max(...prices) / 1000) * 1000 : 0;
    if (!state.filters.priceMax) state.filters.priceMax = state.priceCeiling;

    $('shop-name').textContent = state.shop;
    document.title = state.shop;

    dropSoldFromCart();
    renderCategories();
    renderFilterOptions();
    renderGrid();
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
  }
}

function authHeaders() {
  // Подпись Telegram: по ней сервер узнаёт, кто пришёл, и что данные не подделаны.
  return tg && tg.initData ? { 'X-Telegram-Init-Data': tg.initData } : {};
}

// --- каталог -----------------------------------------------------------------

function visibleProducts() {
  const { category, brands, sizes, condition, priceMax, query } = state.filters;
  const needle = query.trim().toLowerCase();
  return state.catalog.products.filter((product) => {
    if (category && product.category !== category) return false;
    if (brands.size && !brands.has(product.brand)) return false;
    if (condition && product.condition !== condition) return false;
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
    button.onclick = () => {
      state.filters.category = button.dataset.category;
      renderCategories();
      renderGrid();
      haptic('light');
    };
  });
}

function photoTag(product, className = '') {
  const stub = `<div class="stub ${className}">${escapeHtml((product.brand || product.name || '?').slice(0, 2).toUpperCase())}</div>`;
  if (!product.photos || !product.photos.length) return stub;
  return `<img class="${className}" src="${new URL(product.photos[0], PHOTOS)}" alt="${escapeHtml(product.name)}" loading="lazy">`;
}

function renderGrid() {
  const products = visibleProducts();
  const grid = $('grid');
  $('found').textContent = products.length ? `${products.length} ${plural(products.length, 'вещь', 'вещи', 'вещей')}` : '';
  $('empty').hidden = products.length > 0;

  grid.innerHTML = products.map((product) => {
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
  }).join('');

  grid.querySelectorAll('.card').forEach((card) => {
    card.onclick = () => openProduct(Number(card.dataset.id));
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

  const price = $('filter-price');
  price.max = String(state.priceCeiling || 100000);
  price.step = '1000';
  price.value = String(state.filters.priceMax || state.priceCeiling);
  $('price-value').textContent = money(price.value);

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
  const { brands, sizes, condition, priceMax } = state.filters;
  return brands.size > 0 || sizes.size > 0 || Boolean(condition) || (priceMax > 0 && priceMax < state.priceCeiling);
}

function resetFilters() {
  state.filters.brands.clear();
  state.filters.sizes.clear();
  state.filters.condition = '';
  state.filters.priceMax = state.priceCeiling;
  renderFilterOptions();
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
  gallery.scrollLeft = 0;
  renderDots(product.photos.length);
  $('product-brand').textContent = product.brand || product.category_name || '';
  $('product-name').textContent = product.name;
  $('product-price').textContent = money(product.price);
  $('product-old-price').textContent = product.old_price ? money(product.old_price) : '';
  $('product-condition').textContent = product.condition === 'used' ? 'б/у' : 'новое';
  $('product-description').textContent = product.description || '';
  $('product-note').textContent = product.color ? `Цвет: ${product.color}` : '';

  renderSizes();
  show('product');
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

function show(screen) {
  state.screen = screen;
  for (const name of ['catalog', 'product', 'cart', 'checkout', 'done']) {
    $(`screen-${name}`).hidden = name !== screen;
  }
  window.scrollTo(0, 0);
  if (screen === 'cart') renderCart();
  if (screen === 'checkout') renderCheckout();
  syncButtons();
}

function goBack() {
  if (state.screen === 'product' || state.screen === 'cart') show('catalog');
  else if (state.screen === 'checkout') show('cart');
  else show('catalog');
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
  tg.BackButton[state.screen === 'catalog' || state.screen === 'done' ? 'hide' : 'show']();

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

// --- события -----------------------------------------------------------------

function bindEvents() {
  $('cart-button').onclick = () => show('cart');
  $('gallery').onscroll = updateDots;

  let searchTimer = null;
  $('search').oninput = (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.query = event.target.value;
      renderGrid();
    }, 150);
  };

  $('filters-button').onclick = () => { $('filters-sheet').hidden = false; };
  $('filters-close').onclick = () => { $('filters-sheet').hidden = true; };
  $('filters-sheet').onclick = (event) => {
    if (event.target === $('filters-sheet')) $('filters-sheet').hidden = true;
  };
  $('filter-price').oninput = (event) => {
    state.filters.priceMax = Number(event.target.value);
    $('price-value').textContent = money(event.target.value);
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
