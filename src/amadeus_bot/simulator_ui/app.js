const $ = (id) => document.getElementById(id);
let scope = "group";
let actor = 930000001;
let replyTo = null;
let attachments = [];
let lastCount = 0;
let users = [];
let connected = false;
let lastMessageId = 0;

function userName(id) { return users.find(u => u.user_id === id)?.nickname || "Amadeus"; }
function contextRows(rows) {
  return rows.filter(row => row.scope === scope && (scope === "group" || row.recipient_id === actor));
}
function renderUsers() {
  $("users").replaceChildren();
  for (const user of users) {
    const button = document.createElement("button");
    button.className = "person" + (actor === user.user_id ? " active" : "");
    button.title = user.nickname;
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = user.nickname.slice(-1);
    const label = document.createElement("span");
    label.textContent = user.nickname;
    const role = document.createElement("small");
    role.textContent = user.role === "owner" ? "SUPERUSER" : "普通用户";
    button.append(avatar, label, role);
    button.onclick = () => { actor = user.user_id; replyTo = null; refreshContext(); renderUsers(); };
    $("users").append(button);
  }
}
function refreshContext() {
  $("room-title").textContent = scope === "group" ? "模拟群聊" : "模拟私聊";
  $("room-subtitle").textContent = scope === "group" ? "所有用户共享 · OneBot V11" : `${userName(actor)} 与机器人 · OneBot V11`;
  $("actor-label").textContent = `以 ${userName(actor)} 身份发送`;
  document.querySelectorAll(".room").forEach(el => el.classList.toggle("active", el.dataset.scope === scope));
  $("reply").classList.toggle("hidden", replyTo === null);
  $("reply-label").textContent = replyTo === null ? "" : `回复消息 #${replyTo}`;
  renderTimeline(window.allMessages || []);
}
function renderSegment(item, bubble) {
  const type = item.type, data = item.data || {};
  if (type === "text") { bubble.append(document.createTextNode(String(data.text || ""))); return; }
  if (type === "image" && String(data.file || "").startsWith("file:///")) {
    const image = document.createElement("img");
    image.src = `/api/media/${encodeURIComponent(decodeURIComponent(String(data.file).split("/").pop()))}`;
    image.alt = "Bot 生成的图片";
    image.onerror = () => { image.replaceWith(document.createTextNode("[图片不可预览]")); };
    bubble.append(image); return;
  }
  const label = document.createElement("span");
  label.className = "segment";
  label.textContent = type === "at" ? `@${userName(Number(data.qq))}` :
    type === "face" ? `[表情 ${data.id}]` : type === "reply" ? `[回复 #${data.id}]` :
    type === "image" ? "[图片]" : `[${type}]`;
  bubble.append(label);
}
function renderTimeline(rows) {
  const list = contextRows(rows);
  const timeline = $("timeline");
  const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 100;
  timeline.replaceChildren();
  if (!list.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "暂无消息";
    timeline.append(empty);
  }
  for (const row of list) {
    const wrap = document.createElement("article");
    wrap.className = "message" + (!row.bot && row.user_id === actor ? " mine" : "");
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = row.bot ? "A" : userName(row.user_id).slice(-1);
    const body = document.createElement("div");
    body.className = "message-body";
    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("span");
    name.textContent = row.bot ? "Amadeus" : userName(row.user_id);
    const time = document.createElement("span");
    time.textContent = new Date(row.time * 1000).toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"});
    const reply = document.createElement("button");
    reply.textContent = "回复";
    reply.title = `回复消息 #${row.message_id}`;
    reply.onclick = () => { replyTo = row.message_id; refreshContext(); $("text").focus(); };
    meta.append(name, time, reply);
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    for (const segment of row.segments || []) renderSegment(segment, bubble);
    body.append(meta, bubble);
    wrap.append(avatar, body);
    timeline.append(wrap);
  }
  if (atBottom || list.length !== lastCount) timeline.scrollTop = timeline.scrollHeight;
  lastCount = list.length;
}
function renderAttachments() {
  $("attachments").replaceChildren();
  attachments.forEach((item, index) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.textContent = `${item.label} ×`;
    chip.title = "移除此消息段";
    chip.onclick = () => { attachments.splice(index, 1); renderAttachments(); };
    $("attachments").append(chip);
  });
}
async function poll() {
  try {
    const response = await fetch("/api/state");
    const state = await response.json();
    const firstLoad = users.length === 0;
    users = state.users;
    connected = state.connected;
    window.allMessages = state.messages;
    $("signal").classList.toggle("online", connected);
    $("connection").textContent = connected ? "Bot 已连接" : (state.error || "Bot 启动中");
    $("send").disabled = !connected;
    if (firstLoad) { renderUsers(); refreshContext(); }
    const newest = state.messages.at(-1)?.message_id || 0;
    if (newest !== lastMessageId) {
      renderTimeline(state.messages);
      lastMessageId = newest;
    }
  } catch (error) { $("connection").textContent = "模拟器连接失败"; connected = false; }
}
async function post(path, payload) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.detail || `请求失败 ${response.status}`);
  }
  return response.json();
}
async function send() {
  if (!connected) return;
  const text = $("text").value;
  const segments = [];
  if (text) segments.push({type:"text", data:{text}});
  segments.push(...attachments.map(item => item.segment));
  if (!segments.length) return;
  $("send").disabled = true;
  $("error").textContent = "";
  try {
    await post("/api/send", {scope, user_id:actor, segments, reply_to:replyTo});
    $("text").value = "";
    attachments = [];
    replyTo = null;
    renderAttachments();
    await poll();
  } catch (error) { $("error").textContent = error.message; }
  finally { $("send").disabled = !connected; }
}
document.querySelectorAll(".room").forEach(el => el.onclick = () => {
  scope = el.dataset.scope; replyTo = null; lastCount = 0; refreshContext();
});
$("clear-reply").onclick = () => { replyTo = null; refreshContext(); };
$("send").onclick = send;
$("text").onkeydown = event => { if (event.key === "Enter" && event.ctrlKey) { event.preventDefault(); send(); } };
$("at").onclick = () => {
  const id = prompt("要 @ 的模拟用户 ID", String(users.find(u => u.user_id !== actor)?.user_id || actor));
  if (id && users.some(u => u.user_id === Number(id))) {
    attachments.push({label:`@${userName(Number(id))}`, segment:{type:"at", data:{qq:String(id)}}}); renderAttachments();
  }
};
$("face").onclick = () => {
  const id = prompt("QQ 表情 ID", "66");
  if (id && /^\d{1,6}$/.test(id)) {
    attachments.push({label:`表情 ${id}`, segment:{type:"face", data:{id}}}); renderAttachments();
  }
};
$("image").onclick = () => $("image-file").click();
$("image-file").onchange = async event => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 1024 * 1024) { $("error").textContent = "图片不得超过 1 MiB"; return; }
  const base64 = await new Promise(resolve => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.readAsDataURL(file);
  });
  attachments.push({label:file.name, segment:{type:"image", data:{file:`base64://${base64}`}}});
  renderAttachments(); event.target.value = "";
};
$("poke").onclick = async () => {
  $("error").textContent = "";
  try { await post("/api/poke", {user_id:actor}); }
  catch (error) { $("error").textContent = error.message; }
};
poll();
setInterval(poll, 700);
