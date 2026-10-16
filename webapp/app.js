const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

function initData() { return tg.initData || ""; }

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": initData(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function show(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  document.getElementById(id).classList.add("active");
}
function showMenu() { show("screen-menu"); }
function showCreate() { show("screen-create"); }

async function showMyDeals() {
  show("screen-deals");
  const list = document.getElementById("deals-list");
  list.textContent = "Загрузка...";
  try {
    const { deals } = await api("/api/deals");
    if (!deals.length) { list.textContent = "Сделок пока нет."; return; }
    list.innerHTML = deals.map(d => `
      <div class="deal-card">
        <b>#${d.id}</b> — ${d.price} ⭐
        <div class="status">Статус: ${d.status}</div>
        <div class="status">${d.nft_url}</div>
        ${d.status === "paid" ? `<button class="btn primary" onclick="markSent(${d.id})">✅ Я отправил NFT</button>` : ""}
      </div>
    `).join("");
  } catch (e) {
    list.textContent = "Ошибка: " + e.message;
  }
}

async function createDeal() {
  const nft_url = document.getElementById("nft_url").value.trim();
  const price = parseInt(document.getElementById("price").value);
  const ton_wallet = document.getElementById("ton_wallet").value.trim();
  const out = document.getElementById("create-result");
  out.textContent = "Создаём...";
  try {
    const r = await api("/api/deals", {
      method: "POST",
      body: JSON.stringify({ nft_url, price, ton_wallet }),
    });
    out.innerHTML = `✅ Сделка <b>#${r.deal_id}</b> создана!<br>
      <button class="btn primary" onclick="copyText('${r.link}')">📋 Скопировать ссылку</button>`;
  } catch (e) {
    out.textContent = "❌ " + e.message;
  }
}

async function markSent(dealId) {
  try {
    await api(`/api/deals/${dealId}/sent`, { method: "POST" });
    tg.showAlert("✅ Отмечено. Ожидайте подтверждения.");
    showMyDeals();
  } catch (e) {
    tg.showAlert("Ошибка: " + e.message);
  }
}

function copyText(t) {
  navigator.clipboard.writeText(t);
  tg.showAlert("Скопировано!");
}
