"use strict";
const $ = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem("uclean-session") || "";
if (token) sessionStorage.setItem("uclean-session", token);
history.replaceState(null, "", "/");
let current = null, previous = null, busy = false, ended = false, target = 10 * 1024 ** 3;
const selected = new Set();
const labels = {quick:"快速释放", recommended:"继续清理", deep:"深度查找"};
const statusNames = {deleted:"已删除缓存", trashed:"已移入废纸篓", restored:"已恢复", skipped:"已跳过", partial:"部分完成", cancelled:"已取消", pending:"尚未完成"};
const errors = {
  application_running:"相关应用正在运行，退出后重新扫描即可。", files_in_use:"有文件正在使用，请关闭相关应用后重试。",
  scan_incomplete:"扫描未完成，保持原样。可能需要授权或更长的扫描时间。", scan_limit:"达到扫描时间或数量上限，尚有未检查内容。",
  protected_path:"属于保护范围，保持原样。", protected_content:"目录内含受保护内容，保持原样。", symlink_in_target:"包含符号链接，保持原样。",
  target_changed_rescan:"文件在扫描后发生了变化，请重新扫描生成新计划。", plan_expired_or_already_used:"预览已过期或已执行，请重新扫描。",
  select_items_first:"请先勾选要处理的项目。", empty_or_duplicate_selection:"请先勾选要处理的项目。",
  PermissionError:"权限不足，请在 macOS 系统设置中为启动此插件的应用授予对应文件访问权限，再重新扫描。",
  open_file_check_timeout:"检查文件占用超时，保持原样。关闭相关应用后重试。", open_file_check_incomplete:"无法完整检查文件占用，保持原样。",
  process_check_unavailable:"无法检查应用运行状态，保持原样。", record_tampered:"本地计划校验失败，请重新扫描。",
  session_expired_or_missing:"本次面板会话已结束，请从插件重新打开。", user_cancelled:"已取消；已经完成的删除不能撤销。",
  FileExistsError:"原位置已有同名文件，为避免覆盖，没有恢复。", macos_non_root_required:"清理仅支持 macOS 普通用户，请不要使用 sudo。",
};
const explain = key => errors[key] || (key && key.startsWith("no_unchanged_available_items:") ? "所选项目已变化或仍在使用。关闭相关应用后重新检查。" : "未完成本次操作：" + key);
function bytes(n) {
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "−" : ""; n = Math.abs(n);
  if (n >= 1024 ** 3) return sign + (n / 1024 ** 3).toFixed(2) + " GB";
  if (n >= 1024 ** 2) return sign + (n / 1024 ** 2).toFixed(1) + " MB";
  return sign + (n / 1024).toFixed(0) + " KB";
}
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
function button(text, callback, secondary = false) { const b = node("button", text, secondary ? "secondary" : ""); b.type = "button"; b.addEventListener("click", callback); return b; }
function heading(title, description, eyebrow) { $("heading").textContent = title; $("description").textContent = description; $("eyebrow").textContent = eyebrow || "让工作继续"; }
function sum(items) { const wrap = node("div", undefined, "summary"); for (const [title, amount] of items) { const d = node("div"); d.append(node("span", title), node("strong", bytes(amount))); wrap.append(d); } $("content").append(wrap); }
function meter(disk, reclaim = 0) {
  $("meter").hidden = !disk; if (!disk) return;
  $("available").textContent = bytes(disk.free_bytes);
  $("disk-used").style.width = Math.max(0, (disk.used_bytes - reclaim) / disk.total_bytes * 100) + "%";
  $("disk-reclaim").style.width = Math.min(disk.used_bytes, reclaim) / disk.total_bytes * 100 + "%";
  $("disk-total").textContent = "总容量 " + bytes(disk.total_bytes) + " · 蓝色为已识别的可清理分配空间估算";
}
function itemRow(item, choose = false, receipt = null) {
  const row = node("div", undefined, "item");
  const tick = node("input"); tick.type = "checkbox"; tick.id = "item-" + item.id;
  tick.checked = selected.has(item.id); tick.disabled = !choose || !item.eligible;
  tick.setAttribute("aria-label", "选择 " + item.title);
  tick.addEventListener("change", () => { if (tick.checked) selected.add(item.id); else selected.delete(item.id); selectionChanged(); });
  row.append(choose ? tick : node("span", item.status === "deleted" || item.status === "trashed" ? "✓" : "·", "status"));
  const body = node("div"); const title = node(choose ? "label" : "div", item.title, "item-title");
  if (choose) title.htmlFor = tick.id;
  body.append(title);
  if (item.status) body.append(node("div", statusNames[item.status] || item.status, "status"));
  if (item.blocked) body.append(node("div", explain(item.blocked), "detail risk"));
  else if (item.method === "report") body.append(node("div", "仅分析 · 使用专用工具处理", "detail"));
  else if (item.method === "trash") body.append(node("div", "移入废纸篓 · 不立即释放空间", "detail"));
  else if (choose) body.append(node("div", item.level ? "需要确认 · 可能影响离线使用或下次构建" : "可重新下载的缓存 · 永久删除", "detail"));
  const detail = node("details"); detail.append(node("summary", "路径与处理说明"));
  detail.append(node("p", item.path, "path"));
  if (item.consequence) detail.append(node("p", item.consequence));
  if (item.evidence) detail.append(node("p", "依据：" + item.evidence));
  if (item.logical_bytes !== undefined) detail.append(node("p", "逻辑大小 " + bytes(item.logical_bytes) + "；独占分配空间 " + bytes(item.allocated_bytes) + (item.shared_bytes ? "；另有共享硬链接 " + bytes(item.shared_bytes) + "，未计入可释放估算。" : "。")));
  if (item.reason) detail.append(node("p", explain(item.reason), "risk"));
  if (item.recovery_path) detail.append(node("p", "未完成内容保留在：" + item.recovery_path, "path"));
  if (item.trash_path) detail.append(node("p", "废纸篓位置：" + item.trash_path, "path"));
  if (receipt && item.status === "trashed") {
    const restore = button("恢复到原位置", () => send({action:"restore", receipt_id:receipt.id, item_id:item.id}), true);
    restore.classList.add("small"); detail.append(restore);
  }
  body.append(detail); row.append(body, node("div", (item.complete === false ? "≥ " : "") + bytes(item.allocated_bytes), "size"));
  return row;
}
function selectionChanged() {
  if (!current || current.kind !== "scan") return;
  const chosen = current.items.filter(i => selected.has(i.id));
  const label = $("selected-label");
  if (label) label.textContent = "已选 " + chosen.length + " 项 · " + bytes(chosen.reduce((n,i)=>n+i.allocated_bytes,0));
  const b = $("plan-button"); if (b) b.disabled = chosen.length === 0;
}
function action(act) {
  if (act.id === "scan") return send({action:"scan", level:act.level, target_free_bytes:target});
  if (act.id === "plan") return send({action:"plan", scan_id:current.id, items:[...selected]});
  if (act.id === "done") { ended = true; heading("空间整理到这里。", "可以返回原来的工作。需要时再打开 UClean 继续。", "已完成本次操作"); $("actions").replaceChildren(button("重新检查", () => { ended=false; send({action:"scan",level:"quick",target_free_bytes:target}); })); return; }
  if (act.id === "trash") { $("notice").textContent = "在 Finder 中打开废纸篓检查。此插件不自动清空；需要的安装包可以从下方记录恢复。"; return; }
  for (const d of $("content").querySelectorAll("details")) d.open = true;
  $("content").scrollIntoView({behavior:"auto",block:"start"});
}
function render(result) {
  current = result; $("content").replaceChildren(); $("actions").replaceChildren(); $("controls").hidden = true; $("error").hidden = true;
  document.querySelectorAll("[data-stage]").forEach(el => el.classList.toggle("active",el.dataset.stage === result.level));
  if (result.kind === "scan") {
    selected.clear(); for (const i of result.items) if (i.eligible && i.level === 0 && i.method === "delete_cache") selected.add(i.id);
    heading(result.eligible_bytes ? "先让这些空间空出来。" : result.next.scenario === "exhausted" ? "更深的占用，也找到了方向。" : "这一层已检查完。", result.next.message, labels[result.level]);
    meter(result.disk,result.eligible_bytes);
    $("notice").textContent = result.coverage;
    const head = node("div",undefined,"list-head"); head.append(node("span","按风险、可释放容量排序")); const chosen = node("span"); chosen.id="selected-label"; head.append(chosen); $("content").append(head);
    if (!result.items.length) $("content").append(node("p","没有发现这一层的可处理内容。","empty"));
    for (const i of result.items) $("content").append(itemRow(i,true));
    if (result.incomplete_count) {
      const details=node("details",undefined,"warning-list"); details.append(node("summary",result.incomplete_count+" 处未完整检查，未计为 0 B"));
      for (const w of result.warnings) details.append(node("p",w.path+" — "+explain(w.reason)));
      for (const i of result.items.filter(i=>!i.complete)) details.append(node("p",i.path+" — "+i.issues.map(explain).join("；")));
      $("content").append(details);
    }
    result.next.actions.forEach((a,index)=>{ const b=button(a.label,()=>action(a),index>0); if(a.id==="plan")b.id="plan-button"; $("actions").append(b); });
    $("timing").textContent = "本次扫描 " + result.elapsed_seconds + " 秒";
    $("footnote").textContent = "勾选只是准备计划。下一步会展示清理范围，确认后才执行。";
    selectionChanged();
  } else if (result.kind === "plan") {
    heading("确认这一次清理。",result.notice,"执行预览 · 10 分钟内有效");
    sum([["永久删除缓存（估算）",result.delete_bytes],["移入废纸篓（可恢复）",result.trash_bytes]]);
    for(const i of result.items){const row=itemRow(i);row.querySelector("details").open=true;$("content").append(row);}
    if(result.skipped.length) $("content").append(node("p",result.skipped.length+" 项在复查时变化或被占用，已从本计划排除。","risk"));
    $("notice").textContent = "本次只处理以下 "+result.items.length+" 项。不会追加新的清理范围。";
    $("actions").append(button("确认清理这 "+result.items.length+" 项",()=>send({action:"apply",plan_id:result.id,confirmation:result.confirmation})),button("返回重新选择",()=>send({action:"scan",level:result.level,target_free_bytes:target}),true));
    $("footnote").textContent = "缓存删除无法撤销；可能需要重新下载或构建。取消会停止后续处理。";
  } else if (result.kind === "receipt") {
    heading(result.status === "complete" ? "这一步，完成了。" : "已完成能处理的部分。",result.next.message,"清理结果");
    meter(result.after);
    sum([["实测可用空间变化",result.free_delta_bytes],["已删除分配空间",result.deleted_allocated_bytes],["已移入废纸篓",result.trashed_allocated_bytes]]);
    for(const i of result.items)$("content").append(itemRow(i,false,result));
    $("notice").textContent = "空间变化取自执行前后实测，可能同时受到其他应用、APFS 共享数据或快照影响。";
    result.next.actions.forEach((a,index)=>$("actions").append(button(a.label,()=>action(a),index>0)));
    $("footnote").textContent = "记录已保存在本机。缓存没有通用撤销；移入废纸篓的文件可在详情中恢复。";
  }
}
function showBusy(progress={}) {
  busy=true; $("actions").replaceChildren(button("停止后续操作",()=>send({action:"cancel"}),true));
  $("notice").replaceChildren(node("span",undefined,"pulse"),node("span",progress.total ? "已检查 / 处理 "+(progress.checked ?? progress.completed ?? 0)+" / "+progress.total+" 项…" : "正在本机处理，请稍候…"));
  for(const input of $("content").querySelectorAll("input,button"))input.disabled=true;
}
async function send(data) {
  if(data.action!=="cancel")showBusy();
  try {
    const response=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-UClean-Token":token},body:JSON.stringify(data)});
    const value=await response.json(); if(!response.ok)throw Error(value.error);
    await poll();
  } catch(error) { showError(error.message); }
}
function showError(message) {
  busy=false; $("error").hidden=false; $("error").textContent=explain(message);
  $("notice").textContent="未能完成操作；已完成的项目请查看清理记录。";
  if(current?.kind === "receipt" && current.recovery_paths?.length) {
    $("actions").replaceChildren(button("查看未完成项目",()=>action({id:"details"})),button("暂时结束",()=>action({id:"done"}),true));
  } else $("actions").replaceChildren(button("重新扫描",()=>send({action:"scan",level:current?.level||"quick",target_free_bytes:target})),button("暂时结束",()=>action({id:"done"}),true));
}
async function poll() {
  try {
    const response=await fetch("/api/state",{headers:{"X-UClean-Token":token}});
    const state=await response.json(); if(!response.ok)throw Error(state.error);
    if(ended)return;
    if(state.busy){showBusy(state.progress);return;}
    if(state.error){if(state.result && JSON.stringify(state.result)!==previous){previous=JSON.stringify(state.result);render(state.result);}showError(state.error);return;}
    if(state.result && (busy || JSON.stringify(state.result)!==previous)) {previous=JSON.stringify(state.result);render(state.result);}
    busy=false;
  } catch(error) { if(!ended)showError(error.message === "Failed to fetch" ? "本地服务已关闭，请从插件重新打开面板。" : error.message); }
}
$("target").addEventListener("change",()=>target=Number($("target").value)*1024**3);
$("actions").append(button("快速检查",()=>send({action:"scan",level:"quick",target_free_bytes:target})),button("直接深度查找",()=>send({action:"scan",level:"deep",target_free_bytes:target}),true));
if(!token)showError("session_expired_or_missing"); else {poll();setInterval(poll,800);}
