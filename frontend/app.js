/* =======================================================================
   HotelGenie — клиентская логика Telegram Mini App
   Отвечает за: инициализацию Telegram SDK (тема, тактильная отдача,
   кнопки), обращения к REST API бэкенда, отрисовку результатов,
   управление вкладками, фильтрами и избранным.
   ===================================================================== */

"use strict";

/* --- Интеграция с Telegram WebApp ------------------------------------ */
const tg = window.Telegram ? window.Telegram.WebApp : null;
// initData передаётся на бэкенд для проверки подписи и идентификации
// пользователя. Вне Telegram (в браузере) строка пустая — гостевой режим.
const INIT_DATA = tg ? tg.initData : "";

function initTelegram() {
  if (!tg) return;
  tg.ready();
  tg.expand(); // разворачиваем приложение на весь экран

  // Применяем цвета темы Telegram к нашим CSS-переменным.
  applyTheme();
  tg.onEvent("themeChanged", applyTheme);
}

function applyTheme() {
  if (!tg || !tg.themeParams) return;
  const p = tg.themeParams;
  const root = document.documentElement.style;
  if (p.bg_color) root.setProperty("--tg-theme-bg-color", p.bg_color);
  if (p.text_color) root.setProperty("--tg-theme-text-color", p.text_color);
  if (p.hint_color) root.setProperty("--tg-theme-hint-color", p.hint_color);
  if (p.secondary_bg_color)
    root.setProperty("--tg-theme-secondary-bg-color", p.secondary_bg_color);
}

/** Тактильная отдача (если поддерживается устройством). */
function haptic(type = "light") {
  if (tg && tg.HapticFeedback) {
    tg.HapticFeedback.impactOccurred(type);
  }
}

/* --- Обращения к API -------------------------------------------------- */
const api = {
  async smartSearch(query) {
    return fetchJson("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, init_data: INIT_DATA }),
    });
  },
  async filter(params) {
    const qs = new URLSearchParams({ init_data: INIT_DATA });
    Object.entries(params).forEach(([k, v]) => {
      if (v !== null && v !== "" && v !== false) qs.append(k, v);
    });
    return fetchJson(`/api/hotels?${qs.toString()}`);
  },
  async toggleFavorite(hotelId) {
    return fetchJson("/api/favorites/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hotel_id: hotelId, init_data: INIT_DATA }),
    });
  },
  async favorites() {
    return fetchJson(`/api/favorites?init_data=${encodeURIComponent(INIT_DATA)}`);
  },
  async history() {
    return fetchJson(`/api/history?init_data=${encodeURIComponent(INIT_DATA)}`);
  },
  async cities() {
    return fetchJson("/api/cities");
  },
};

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    throw new Error(detail.detail || `Ошибка ${resp.status}`);
  }
  return resp.json();
}

/* --- Управление загрузкой -------------------------------------------- */
const loaderMessages = [
  "ИИ изучает ваш запрос…",
  "Подбираем удобства…",
  "Сравниваем цены и рейтинги…",
];
function showLoader() {
  const el = document.getElementById("loader");
  const txt = document.getElementById("loader-text");
  el.hidden = false;
  let i = 0;
  txt.textContent = loaderMessages[0];
  el._timer = setInterval(() => {
    i = (i + 1) % loaderMessages.length;
    txt.textContent = loaderMessages[i];
  }, 1100);
}
function hideLoader() {
  const el = document.getElementById("loader");
  el.hidden = true;
  if (el._timer) clearInterval(el._timer);
}

/* --- Отрисовка карточек ----------------------------------------------- */
const favoriteState = new Set();

function renderHotels(container, hotels) {
  container.innerHTML = "";
  if (!hotels || hotels.length === 0) {
    container.innerHTML =
      '<div class="empty"><span class="empty__emoji">🔍</span>' +
      "Ничего не найдено. Попробуйте смягчить условия поиска.</div>";
    return;
  }
  const tpl = document.getElementById("hotel-template");
  hotels.forEach((h, idx) => {
    const node = tpl.content.cloneNode(true);
    const card = node.querySelector(".hotel");
    card.style.animationDelay = `${idx * 45}ms`;

    const media = node.querySelector(".hotel__media");
    media.textContent = h.image;
    if (h.photo) {
      const img = new Image();
      img.alt = h.name;
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.onload = () => { media.textContent = ""; media.classList.add("hotel__media--photo"); media.appendChild(img); };
      img.src = h.photo;
    }
    const links = document.createElement("div");
    links.className = "hotel__links";
    const map = document.createElement("a");
    map.href = h.map_url; map.target = "_blank"; map.rel = "noopener"; map.textContent = "🗺 На карте";
    links.appendChild(map);
    if (h.source_url) {
      const src = document.createElement("a");
      src.href = h.source_url; src.target = "_blank"; src.rel = "noopener"; src.textContent = "📷 Источник фото";
      links.appendChild(src);
    }
    node.querySelector(".hotel__desc").after(links);
    node.querySelector(".hotel__name").textContent = h.name;
    node.querySelector(".hotel__stars").textContent = "★".repeat(h.stars);
    node.querySelector(".hotel__city").textContent =
      "📍 " + h.city + (h.near_sea ? " · у моря" : "");
    node.querySelector(".hotel__desc").textContent = h.description;

    const amen = node.querySelector(".hotel__amenities");
    h.amenities.forEach((a) => {
      const span = document.createElement("span");
      span.className = "amenity";
      span.textContent = a;
      amen.appendChild(span);
    });

    node.querySelector(".hotel__price").innerHTML =
      `${h.price.toLocaleString("ru-RU")} ₽ <small>/ ночь</small>`;
    node.querySelector(".hotel__rating").textContent = "⭐ " + h.rating;

    const favBtn = node.querySelector(".hotel__fav");
    const isFav = favoriteState.has(h.id);
    favBtn.textContent = isFav ? "♥" : "♡";
    favBtn.classList.toggle("active", isFav);
    favBtn.addEventListener("click", () => onToggleFavorite(h.id, favBtn));

    container.appendChild(node);
  });
}

async function onToggleFavorite(hotelId, btn) {
  haptic("medium");
  try {
    const { is_favorite } = await api.toggleFavorite(hotelId);
    if (is_favorite) favoriteState.add(hotelId);
    else favoriteState.delete(hotelId);
    btn.textContent = is_favorite ? "♥" : "♡";
    btn.classList.toggle("active", is_favorite);
  } catch (e) {
    alert("Не удалось обновить избранное: " + e.message);
  }
}

/* --- Сценарий умного поиска ------------------------------------------- */
async function runSearch(query) {
  if (!query.trim()) return;
  document.getElementById("query").value = query;
  haptic("light");
  showLoader();
  try {
    const data = await api.smartSearch(query);

    // Обновляем множество избранного из ответа сервера.
    favoriteState.clear();
    data.favorite_ids.forEach((id) => favoriteState.add(id));

    // Карточка рекомендации ИИ.
    const aiCard = document.getElementById("ai-card");
    aiCard.hidden = false;
    document.getElementById("ai-text").textContent = data.recommendation;
    const badge = document.getElementById("ai-badge");
    if (data.used_ai) {
      badge.textContent = "✦ Рекомендация ИИ";
      badge.classList.remove("fallback");
    } else {
      badge.textContent = "⚙ Быстрый подбор";
      badge.classList.add("fallback");
    }

    // Чипсы с распознанными критериями.
    const crit = document.getElementById("ai-criteria");
    crit.innerHTML = "";
    const c = data.criteria;
    const chips = [];
    if (c.city) chips.push("📍 " + c.city);
    if (c.guests) chips.push("👥 " + c.guests);
    if (c.max_price) chips.push("до " + c.max_price + " ₽");
    if (c.min_stars) chips.push(c.min_stars + "★+");
    if (c.near_sea) chips.push("🌊 у моря");
    (c.amenities || []).forEach((a) => chips.push("✓ " + a));
    chips.forEach((text) => {
      const span = document.createElement("span");
      span.className = "crit-chip";
      span.textContent = text;
      crit.appendChild(span);
    });

    renderHotels(document.getElementById("results"), data.hotels);
    loadHistory(); // обновим список недавних запросов
  } catch (e) {
    alert("Ошибка поиска: " + e.message);
  } finally {
    hideLoader();
  }
}

/* --- Ручные фильтры --------------------------------------------------- */
let selectedStars = 0;

async function applyFilters() {
  haptic("light");
  showLoader();
  try {
    const params = {
      city: document.getElementById("f-city").value,
      max_price: document.getElementById("f-price").value,
      min_stars: selectedStars || "",
      near_sea: document.getElementById("f-sea").checked,
    };
    const data = await api.filter(params);
    favoriteState.clear();
    data.favorite_ids.forEach((id) => favoriteState.add(id));
    document.getElementById("ai-card").hidden = true;
    renderHotels(document.getElementById("results"), data.hotels);
  } catch (e) {
    alert("Ошибка фильтрации: " + e.message);
  } finally {
    hideLoader();
  }
}

/* --- История ---------------------------------------------------------- */
async function loadHistory() {
  try {
    const { queries } = await api.history();
    const wrap = document.getElementById("history");
    const chips = document.getElementById("history-chips");
    if (!queries || queries.length === 0) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    chips.innerHTML = "";
    queries.forEach((q) => {
      const btn = document.createElement("button");
      btn.className = "chip chip--history";
      btn.textContent = q.length > 36 ? q.slice(0, 34) + "…" : q;
      btn.addEventListener("click", () => runSearch(q));
      chips.appendChild(btn);
    });
  } catch (e) {
    /* история необязательна — игнорируем ошибку */
  }
}

/* --- Избранное (вкладка) ---------------------------------------------- */
async function loadFavorites() {
  showLoader();
  try {
    const { hotels } = await api.favorites();
    favoriteState.clear();
    hotels.forEach((h) => favoriteState.add(h.id));
    const container = document.getElementById("fav-results");
    if (!hotels.length) {
      container.innerHTML =
        '<div class="empty"><span class="empty__emoji">❤</span>' +
        "Пока пусто. Добавляйте отели кнопкой-сердечком на карточке.</div>";
    } else {
      renderHotels(container, hotels);
    }
  } catch (e) {
    alert("Не удалось загрузить избранное: " + e.message);
  } finally {
    hideLoader();
  }
}

/* --- Переключение вкладок --------------------------------------------- */
function switchTab(tab) {
  haptic("light");
  document.getElementById("tab-search").hidden = tab !== "search";
  document.getElementById("tab-fav").hidden = tab !== "fav";
  document.querySelectorAll(".tabbar__btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  if (tab === "fav") loadFavorites();
}

/* --- Заполнение списка городов ---------------------------------------- */
async function loadCities() {
  try {
    const { cities } = await api.cities();
    const select = document.getElementById("f-city");
    cities.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      select.appendChild(opt);
    });
  } catch (e) {
    /* список городов необязателен */
  }
}

/* --- Навешивание обработчиков ----------------------------------------- */
function bindEvents() {
  document
    .getElementById("search-btn")
    .addEventListener("click", () =>
      runSearch(document.getElementById("query").value)
    );

  // Подсказки и переключатель фильтров.
  document.querySelectorAll(".chip--ghost").forEach((chip) => {
    chip.addEventListener("click", () => runSearch(chip.textContent));
  });

  document.getElementById("filters-toggle").addEventListener("click", () => {
    const f = document.getElementById("filters");
    f.hidden = !f.hidden;
    haptic("light");
  });

  // Слайдер цены.
  const price = document.getElementById("f-price");
  price.addEventListener("input", () => {
    document.getElementById("f-price-val").textContent = Number(
      price.value
    ).toLocaleString("ru-RU");
  });

  // Выбор звёзд.
  document.querySelectorAll("#f-stars button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document
        .querySelectorAll("#f-stars button")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      selectedStars = Number(btn.dataset.v);
    });
  });

  document
    .getElementById("filters-apply")
    .addEventListener("click", applyFilters);

  // Нижняя навигация.
  document.querySelectorAll(".tabbar__btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Поиск по нажатию Enter (без Shift) в поле ввода.
  document.getElementById("query").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      runSearch(e.target.value);
    }
  });
}

/* --- Инициализация ---------------------------------------------------- */
window.addEventListener("DOMContentLoaded", () => {
  initTelegram();
  bindEvents();
  loadCities();
  loadHistory();
});
