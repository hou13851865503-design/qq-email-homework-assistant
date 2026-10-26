const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { roster: [], tasks: [], current: null, reminderDrafts: [], rosterImportRows: [], announcementSelection: new Set(), connected: false, toastTimer: null };

function esc(value = '') {
  return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || 'GET',
    headers: options.body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: 'no-store',
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('error', error);
  node.classList.add('visible');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove('visible'), 3600);
}

function setBusy(form, busy, label) {
  form.classList.toggle('loading', busy);
  const button = $('button[type="submit"]', form);
  if (button) {
    if (!button.dataset.label) button.dataset.label = button.innerHTML;
    button.disabled = busy;
    button.innerHTML = busy ? `<span class="spinner"></span>${esc(label || '处理中…')}` : button.dataset.label;
  }
}

function localIsoDateTime(value) {
  if (!value) return '';
  return value.length === 16 ? `${value}:00` : value;
}

function formatDate(value, time = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', time ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' } : { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

async function refresh() {
  const data = await api('/api/state');
  state.roster = data.roster;
  state.tasks = data.tasks;
  state.announcements = data.announcements || [];
  state.connected = data.connected;
  $('#account-label').textContent = data.connected ? data.account : '连接邮箱';
  $('#account-dot').classList.toggle('connected', data.connected);
  renderGroupFilter(data.groups);
  renderRoster();
  renderHistory();
  renderAnnouncementHistory();
}

function renderGroupFilter(groups) {
  const select = $('#group-filter');
  const previous = select.value;
  select.innerHTML = '<option value="">全部在册成员</option>' + groups.map(group => `<option value="${esc(group)}">${esc(group)}</option>`).join('');
  if (groups.includes(previous)) select.value = previous;
  const announcementGroup = $('#announcement-group');
  const announcementPrevious = announcementGroup.value;
  announcementGroup.innerHTML = '<option value="">全部分类</option><option value="__none__">未分类</option>' + groups.map(group => `<option value="${esc(group)}">${esc(group)}</option>`).join('');
  if (groups.includes(announcementPrevious) || announcementPrevious === '__none__') announcementGroup.value = announcementPrevious;
}

function renderRoster() {
  const search = ($('#roster-search')?.value || '').trim().toLowerCase();
  const filtered = state.roster.filter(member => [member.name, member.qq_number, member.student_id, member.group_name, member.email].some(value => String(value || '').toLowerCase().includes(search)));
  $('#roster-count').textContent = `${state.roster.length} 位成员`;
  $('#roster-table').innerHTML = filtered.length ? filtered.map(member => `
    <tr>
      <td>${esc(member.name)}</td><td>${esc(member.qq_number)}</td><td>${esc(member.student_id || '—')}</td><td>${esc(member.group_name || '—')}</td>
      <td>${esc(member.email || '—')}</td><td>${member.email ? `<span class="status-pill ${member.email_verified ? 'status-submitted' : 'status-missing'}">${member.email_verified ? '已核实' : '待核实'}</span>` : '<span class="muted-text">未登记</span>'}</td>
      <td><button class="text-action" data-remove-member="${esc(member.qq_number)}" title="移除成员">移除</button></td>
    </tr>`).join('') : '<tr><td colspan="7" class="table-empty">没有匹配的成员记录</td></tr>';
}

function announcementEligible(member) {
  return Boolean(member.active && member.email_verified && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(member.email || '')));
}

function visibleAnnouncementMembers() {
  const group = $('#announcement-group').value;
  const search = ($('#announcement-search').value || '').trim().toLowerCase();
  return state.roster.filter(member => {
    if (!announcementEligible(member)) return false;
    if (group === '__none__' && member.group_name) return false;
    if (group && group !== '__none__' && member.group_name !== group) return false;
    return [member.name, member.qq_number, member.student_id, member.email, member.group_name]
      .some(value => String(value || '').toLowerCase().includes(search));
  });
}

function updateAnnouncementSelection() {
  const members = visibleAnnouncementMembers();
  const group = $('#announcement-group').value;
  const inGroup = state.roster.filter(member => {
    if (group === '__none__') return !member.group_name;
    return !group || member.group_name === group;
  });
  const skipped = inGroup.filter(member => !announcementEligible(member)).length;
  const selected = state.announcementSelection.size;
  $('#announcement-selection-count').textContent = `已选 ${selected} 人 · 当前筛选 ${members.length} 人邮箱有效 · ${skipped} 人因邮箱未登记或未核实而跳过`;
  $('#announcement-preview-count').textContent = selected ? `将向 ${selected} 位成员分别发送，收件人之间互不可见。` : '尚未选择收件人。';
  const canSend = state.connected && selected > 0 && $('#announcement-subject').value.trim() && $('#announcement-body').value.trim();
  $('#announcement-send').disabled = !canSend;
  $('#announcement-send').textContent = selected ? `发送通知 (${selected} 人)` : '发送通知';
}

function renderAnnouncementRecipients() {
  const members = visibleAnnouncementMembers();
  $('#announcement-recipients').innerHTML = members.length ? members.map(member => `
    <label class="announcement-recipient">
      <input type="checkbox" data-announcement-qq="${esc(member.qq_number)}" ${state.announcementSelection.has(member.qq_number) ? 'checked' : ''}>
      <span><strong>${esc(member.name)} · ${esc(member.qq_number)}</strong><small>${esc(member.email)} · ${esc(member.group_name || '未分类')}</small></span>
    </label>`).join('') : '<div class="reminder-empty">当前筛选下没有可发送成员；请检查分类筛选或核实成员邮箱。</div>';
  updateAnnouncementSelection();
}

function renderAnnouncementHistory() {
  const container = $('#announcement-history');
  if (!container) return;
  container.innerHTML = state.announcements?.length ? state.announcements.map(item => `
    <button class="history-item announcement-history-item" data-announcement-detail="${item.id}">
      <span class="history-main"><strong>${esc(item.subject)}</strong><small>${esc(item.created_at)} · ${esc(item.group_name || '全部分类')}</small></span>
      <span class="history-count">${item.sent_count || 0}/${item.recipient_count} 已发送${item.failed_count ? ` · ${item.failed_count} 失败` : ''}</span>
    </button>`).join('') : '<div class="history-empty">尚无批量通知记录。</div>';
}

function renderHistory() {
  const container = $('#task-history');
  container.innerHTML = state.tasks.length ? state.tasks.map(task => `
    <button class="history-item" data-load-task="${task.id}">
      <span class="history-main"><strong>${esc(task.name)}</strong><small>${esc(task.start_date)} 至 ${esc(task.end_date)} · ${esc(task.assignment_label || '未设固定标识')} · ${esc(task.group_name || '全部成员')}</small></span>
      <span class="history-count">${task.counts.submitted}/${task.counts.expected} 已交 · ${task.counts.files} 份 PDF</span>
    </button>`).join('') : '<div class="history-empty">完成第一次扫描后，任务记录会显示在这里。</div>';
}

function setCount(id, value) { $(id).textContent = String(value); }

function renderTask(data) {
  state.current = data;
  const counts = data.counts;
  setCount('#stat-expected', counts.expected);
  setCount('#stat-submitted', counts.submitted);
  setCount('#stat-missing', counts.missing);
  setCount('#stat-late', counts.late);
  setCount('#stat-files', counts.files);
  setCount('#member-tab-count', data.members.length);
  setCount('#file-tab-count', counts.files);
  setCount('#unmatched-tab-count', data.unmatched.length);
  $('#result-kicker').textContent = `${data.task.start_date} 至 ${data.task.end_date}`;
  $('#result-title').textContent = data.task.name;
  renderAutoRemind(data.task);
  $('#empty-state').hidden = true;
  $('#result-content').hidden = false;
  $('#result-actions').hidden = false;
  $('#desktop-export-button').disabled = counts.files === 0;
  $('#members-table').innerHTML = data.members.length ? data.members.map(member => `
    <tr>
      <td>${esc(member.name)}</td><td>${esc(member.student_id || '—')}</td><td>${esc(member.qq_number)}</td><td>${esc(member.group_name || '—')}</td>
      <td><span class="status-pill status-${member.status}">${member.status === 'missing' ? '未提交' : member.status === 'late' ? '迟交' : '已提交'}</span></td>
      <td>${member.files.length ? member.files.map(file => `<span title="${esc(file.filename)}">${esc(file.filename)}</span>`).join('<br>') : '—'}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="table-empty">花名册为空，请先到“提交名单”添加成员。</td></tr>';
  $('#files-table').innerHTML = data.attachments.length ? data.attachments.map(file => `
    <tr>
      <td><input type="checkbox" class="file-choice" value="${file.id}" aria-label="选择 ${esc(file.filename)}"></td>
      <td><div class="file-name"><span class="pdf-mark">PDF</span><span title="${esc(file.filename)}">${esc(file.filename)}</span></div></td>
      <td title="${esc(file.from_addr)}">${esc(file.from_name || file.from_addr || '未知发件人')}</td><td>${esc(formatDate(file.received_at, true))}</td>
      <td class="truncate" title="${esc(file.subject)}">${esc(file.subject || '(无主题)')}</td>
    </tr>`).join('') : '<tr><td colspan="5" class="table-empty">没有符合条件的 PDF 附件</td></tr>';
  $('#unmatched-table').innerHTML = data.unmatched.length ? data.unmatched.map(message => `
    <tr><td>${esc(message.from_addr || '未知')}</td><td>${esc(message.from_name || '—')}</td><td>${esc(formatDate(message.received_at, true))}</td><td class="truncate" title="${esc(message.subject)}">${esc(message.subject || '(无主题)')}</td><td>${esc(message.match_method || '身份待确认')}</td></tr>`).join('') : '<tr><td colspan="5" class="table-empty">没有未匹配的邮件</td></tr>';
  $('#select-all-files').checked = false;
  $('#download-button').disabled = true;
  $$('.result-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.resultTab === 'members'));
  $$('.tab-content').forEach(tab => tab.classList.toggle('active', tab.id === 'tab-members'));
  renderSubmissionVis(data);
}

// ---- 自动提醒：复选框 / 徽标 / 状态行三处同步 ----
function setAutoRemindHint(on, sent) {
  const el = $('#auto-remind-state');
  if (!el) return;
  el.dataset.state = sent ? 'sent' : (on ? 'on' : 'off');
  el.textContent = sent ? '本次提醒已自动发送' : (on ? '已开启：截止后自动发一次' : '已关闭：不会自动提醒');
}

async function toggleAutoRemind(taskId, enabled, trigger) {
  try {
    const data = await api(`/api/tasks/${taskId}/auto-remind`, { method: 'POST', body: { enabled } });
    if (state.current && state.current.task && state.current.task.id === taskId) state.current = data;
    renderAutoRemind(data.task);
    toast(enabled ? '已开启自动提醒' : '已关闭自动提醒');
  } catch (error) {
    if (trigger === 'checkbox') { $('#auto-remind').checked = !enabled; setAutoRemindHint(!enabled, false); }
    const badge = $('#auto-remind-badge');
    if (badge) badge.disabled = false;
    toast(error.message || '操作失败，请重试', true);
  }
}

function renderAutoRemind(task) {
  const on = !!task.auto_remind;
  const sent = !!task.auto_reminded_at;
  const box = $('#auto-remind');
  if (box) box.checked = on;
  setAutoRemindHint(on, sent);
  const badge = $('#auto-remind-badge');
  if (!badge) return;
  badge.hidden = false;
  badge.classList.toggle('off', !on && !sent);
  if (sent) {
    badge.textContent = '自动提醒已发送';
    badge.disabled = true;
    badge.title = '该任务截止后的提醒已自动发送过一次，不再变更';
    badge.onclick = null;
  } else {
    badge.textContent = on ? '自动提醒已开启 · 点击关闭' : '自动提醒已关闭 · 点击开启';
    badge.disabled = false;
    badge.title = on ? '点击后，截止后不再自动提醒未交成员' : '点击后，截止后自动给未交成员发一次提醒（需保持邮箱连接）';
    badge.onclick = async () => {
      badge.disabled = true;
      await toggleAutoRemind(task.id, !on, 'badge');
    };
  }
}

function renderSubmissionVis(data) {
  const vis = $('#submission-vis');
  if (!data || !data.members || !data.members.length) { vis.hidden = true; return; }
  vis.hidden = false;
  const members = data.members;
  const onTime = members.filter(m => m.status === 'submitted').length;
  const late = members.filter(m => m.status === 'late').length;
  const missing = members.filter(m => m.status === 'missing').length;
  const submitted = onTime + late;
  const total = members.length;
  const pct = total ? Math.round(submitted / total * 100) : 0;

  const canvas = $('#vis-canvas');
  const size = canvas.width || 160;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  canvas.style.width = size + 'px';
  canvas.style.height = size + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);
  const cx = size / 2, cy = size / 2, r = size / 2 - 4, rin = r - 22;
  const segments = [
    { label: '已提交', value: onTime, color: '#24715e' },
    { label: '迟交', value: late, color: '#39748c' },
    { label: '未提交', value: missing, color: '#b5792d' },
  ];
  const sum = onTime + late + missing || 1;
  let start = -Math.PI / 2;
  segments.forEach(s => {
    const angle = s.value / sum * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, start + angle);
    ctx.closePath();
    ctx.fillStyle = s.color;
    ctx.fill();
    start += angle;
  });
  ctx.beginPath();
  ctx.arc(cx, cy, rin, 0, Math.PI * 2);
  ctx.fillStyle = '#fff';
  ctx.fill();
  $('#vis-pct').textContent = pct + '%';
  $('#vis-legend').innerHTML = segments.map(s =>
    `<div class="vis-legend-item"><span class="vis-swatch" style="background:${s.color}"></span>${s.label}<strong>${s.value}</strong></div>`).join('');

  $('#vis-overall').innerHTML = `<strong>${submitted}/${total}</strong> (${pct}%)`;
  $('#vis-progress').style.width = pct + '%';

  const groups = {};
  members.forEach(m => {
    const g = m.group_name || '未分组';
    const item = groups[g] || (groups[g] = { total: 0, submitted: 0, late: 0 });
    item.total += 1;
    if (m.status === 'submitted') item.submitted += 1;
    else if (m.status === 'late') { item.submitted += 1; item.late += 1; }
  });
  const names = Object.keys(groups).sort((a, b) => (groups[b].submitted / groups[b].total) - (groups[a].submitted / groups[a].total));
  $('#vis-classes').innerHTML = names.map(g => {
    const item = groups[g];
    const rp = item.total ? Math.round(item.submitted / item.total * 100) : 0;
    return `<div class="vis-class-row"><div class="vis-class-top"><span>${esc(g)}</span><strong>${item.submitted}/${item.total}${item.late ? ` · 迟 ${item.late}` : ''}</strong></div><div class="progress-track sm"><div class="progress-fill" style="width:${rp}%"></div></div></div>`;
  }).join('') || '<div class="vis-empty">没有班级分组数据</div>';
}

function showView(view) {
  $$('.view').forEach(node => node.classList.toggle('active', node.id === `view-${view}`));
  $$('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.view === view));
  $('#page-title').textContent = view === 'overview' ? '统计任务' : '提交名单';
}

$('#connection-dialog').addEventListener('click', event => {
  if (event.target === $('#connection-dialog')) $('#connection-dialog').close();
});
$('#reminder-dialog').addEventListener('click', event => {
  if (event.target === $('#reminder-dialog')) $('#reminder-dialog').close();
});

$('#account-open').addEventListener('click', async () => {
  const dialog = $('#connection-dialog');
  try {
    const data = await api('/api/state');
    $('#mail-account').value = data.account || '';
  } catch { }
  dialog.showModal();
});

$('#connection-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  setBusy(form, true, '验证邮箱中…');
  try {
    const account = $('#mail-account').value.trim();
    const authCode = $('#mail-auth-code').value;
    await api('/api/connect', { method: 'POST', body: { account, auth_code: authCode } });
    $('#mail-auth-code').value = '';
    $('#connection-dialog').close();
    await refresh();
    toast('邮箱连接成功。授权码已留在当前运行内存中。');
  } catch (error) { toast(error.message, true); }
  finally { setBusy(form, false); }
});

$('#disconnect-button').addEventListener('click', async () => {
  await api('/api/disconnect', { method: 'POST', body: {} });
  $('#mail-auth-code').value = '';
  await refresh();
  toast('已断开邮箱连接');
});

$('#scan-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const fields = $$('input[name="keyword-field"]:checked').map(input => input.value);
  if (($('#keywords').value.trim() || $('#assignment-label').value.trim()) && !fields.length) return toast('请至少选择一个检索位置', true);
  const keywords = $('#keywords').value.split(/[，,\n]/).map(value => value.trim()).filter(Boolean);
  const body = {
      name: $('#task-name').value.trim(), start_date: $('#start-date').value, end_date: $('#end-date').value,
      assignment_label: $('#assignment-label').value.trim(), match_by_student_id: $('#match-by-student-id').checked,
      auto_remind: $('#auto-remind').checked,
      due_at: localIsoDateTime($('#due-at').value), group_name: $('#group-filter').value,
    keywords, keyword_mode: $('input[name="keyword-mode"]:checked').value, keyword_fields: fields,
  };
  setBusy(form, true, '正在按条件扫描…');
  try {
    const data = await api('/api/scan', { method: 'POST', body });
    renderTask(data);
    await refresh();
    showView('overview');
    toast(`统计完成：找到 ${data.counts.files} 份 PDF，${data.counts.missing} 人未提交`);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(form, false); }
});

// 勾选/取消「截止后自动提醒」：立即更新状态行；若当前已加载任务，则直接改该任务并同步徽标
$('#auto-remind').addEventListener('change', async event => {
  const on = event.target.checked;
  setAutoRemindHint(on, false);
  const task = state.current && state.current.task;
  if (!task) return;                       // 还没有任务：只记住选择，下次扫描生效
  event.target.disabled = true;
  try { await toggleAutoRemind(task.id, on, 'checkbox'); }
  finally { event.target.disabled = false; }
});

$('#roster-form').addEventListener('submit', async event => {
  event.preventDefault();
  const body = {
    qq_number: $('#roster-qq').value.trim(), name: $('#roster-name').value.trim(),
    student_id: $('#roster-student-id').value.trim(),
    group_name: $('#roster-group').value.trim(), email: $('#roster-email').value.trim(),
    email_verified: $('#roster-verified').checked,
  };
  try {
    await api('/api/roster', { method: 'POST', body });
    event.currentTarget.reset();
    await refresh();
    toast('成员信息已保存');
  } catch (error) { toast(error.message, true); }
});

$('#roster-search').addEventListener('input', renderRoster);

$('#roster-table').addEventListener('click', async event => {
  const button = event.target.closest('[data-remove-member]');
  if (!button) return;
  const qq = button.dataset.removeMember;
  const member = state.roster.find(row => row.qq_number === qq);
  if (!member || !confirm(`确定从名单移除 ${member.name}（${qq}）？历史统计记录会保留。`)) return;
  try {
    await api('/api/roster', { method: 'POST', body: { action: 'delete', qq_number: qq } });
    await refresh();
    toast('成员已移除');
  } catch (error) { toast(error.message, true); }
});

function rosterImportError(index) {
  const row = state.rosterImportRows[index];
  if (!row.name.trim() || !row.student_id.trim() || !row.qq_number.trim()) return '姓名、学号和 QQ 号不能为空';
  if (!/^\d{5,12}$/.test(row.qq_number.trim())) return 'QQ 号应为 5 至 12 位数字';
  if (state.rosterImportRows.some((other, otherIndex) => otherIndex !== index && other.qq_number.trim() === row.qq_number.trim())) return '文件中存在重复 QQ 号';
  if (state.rosterImportRows.some((other, otherIndex) => otherIndex !== index && other.student_id.trim() === row.student_id.trim())) return '文件中存在重复学号';
  return '';
}

function updateRosterImportStatus() {
  let valid = 0;
  let selected = 0;
  state.rosterImportRows.forEach((row, index) => {
    const element = $(`[data-import-row="${index}"]`, $('#roster-import-table'));
    if (!element) return;
    const error = rosterImportError(index);
    const checkbox = $('[data-import-select]', element);
    const errorCell = $('.import-error', element);
    const isValid = !error;
    if (isValid) valid += 1;
    checkbox.disabled = !isValid;
    if (!isValid) checkbox.checked = false;
    row.selected = checkbox.checked && isValid;
    if (checkbox.checked && isValid) selected += 1;
    errorCell.textContent = error || '可导入';
    errorCell.classList.toggle('muted-text', !error);
  });
  $('#roster-import-summary').textContent = `${state.rosterImportRows.length} 条记录 · ${valid} 条可导入 · ${selected} 条已选`;
  $('#roster-import-submit').disabled = selected === 0;
}

function showRosterImportPreview(result) {
  state.rosterImportRows = result.rows.map(row => ({
    row_number: row.row_number,
    name: row.name || '',
    student_id: row.student_id || '',
    qq_number: row.qq_number || '',
    group_name: row.group_name || '',
    uses_default_group: !row.group_name,
    selected: !row.error,
  }));
  $('#roster-import-title').textContent = `核对成员记录 · ${result.filename}`;
  $('#roster-import-default-group').value = '';
  $('#roster-import-table').innerHTML = state.rosterImportRows.map((row, index) => `
    <tr data-import-row="${index}">
      <td><input type="checkbox" data-import-select aria-label="选择第 ${index + 1} 条记录" ${row.selected ? 'checked' : ''}></td>
      <td><input data-import-field="name" aria-label="姓名" value="${esc(row.name)}"></td>
      <td><input data-import-field="student_id" aria-label="学号" value="${esc(row.student_id)}"></td>
      <td><input data-import-field="qq_number" aria-label="QQ 号" inputmode="numeric" value="${esc(row.qq_number)}"></td>
      <td><input data-import-field="group_name" aria-label="分类" value="${esc(row.group_name)}"></td>
      <td class="import-error"></td>
    </tr>`).join('');
  updateRosterImportStatus();
  $('#roster-import-dialog').showModal();
}

$('#document-import').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error('文件不能超过 8 MB');
    const response = await fetch('/api/roster/import-preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', 'X-Upload-Filename': encodeURIComponent(file.name) },
      body: file,
      cache: 'no-store',
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '文件解析失败');
    showRosterImportPreview(result);
  } catch (error) { toast(error.message, true); }
  finally { event.target.value = ''; }
});

$('#roster-import-default-group').addEventListener('input', event => {
  const value = event.target.value.trim();
  state.rosterImportRows.forEach((row, index) => {
    if (!row.uses_default_group) return;
    row.group_name = value;
    const input = $(`[data-import-row="${index}"] [data-import-field="group_name"]`, $('#roster-import-table'));
    if (input) input.value = value;
  });
  updateRosterImportStatus();
});

$('#roster-import-table').addEventListener('input', event => {
  const field = event.target.dataset.importField;
  const rowElement = event.target.closest('[data-import-row]');
  if (!field || !rowElement) return;
  const row = state.rosterImportRows[Number(rowElement.dataset.importRow)];
  row[field] = event.target.value.trim();
  if (field === 'group_name') row.uses_default_group = false;
  updateRosterImportStatus();
});

$('#roster-import-table').addEventListener('change', event => {
  if (!event.target.matches('[data-import-select]')) return;
  updateRosterImportStatus();
});

$('#roster-import-cancel').addEventListener('click', () => $('#roster-import-dialog').close());

$('#roster-import-form').addEventListener('submit', async event => {
  event.preventDefault();
  const rows = state.rosterImportRows.filter((row, index) => row.selected && !rosterImportError(index))
    .map(({ name, student_id, qq_number, group_name }) => ({ name, student_id, qq_number, group_name }));
  if (!rows.length) return toast('请先选择有效记录', true);
  const button = $('#roster-import-submit');
  button.disabled = true;
  try {
    const result = await api('/api/roster', { method: 'POST', body: { action: 'import-parsed', rows } });
    $('#roster-import-dialog').close();
    await refresh();
    toast(`已导入 ${result.count} 位成员，新增 ${result.created} 位，更新 ${result.updated} 位`);
  } catch (error) {
    toast(error.message, true);
    updateRosterImportStatus();
  }
});

$('#csv-import').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const csv = await file.text();
    const result = await api('/api/roster', { method: 'POST', body: { action: 'import', csv } });
    await refresh();
    toast(`已导入或更新 ${result.count} 位成员`);
  } catch (error) { toast(error.message, true); }
  finally { event.target.value = ''; }
});

function downloadText(filename, text, type = 'text/csv;charset=utf-8') {
  const blob = new Blob([new Uint8Array([0xef, 0xbb, 0xbf]), text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvCell(value) { return `"${String(value ?? '').replaceAll('"', '""')}"`; }

$('#roster-export').addEventListener('click', () => {
  const rows = [['qq_number', 'name', 'student_id', 'group_name', 'email', 'email_verified'], ...state.roster.map(member => [member.qq_number, member.name, member.student_id, member.group_name, member.email, member.email_verified ? 'true' : 'false'])];
  downloadText('提交名单.csv', rows.map(row => row.map(csvCell).join(',')).join('\r\n'));
});

$('#announcement-open').addEventListener('click', () => {
  if (!state.connected) {
    toast('请先连接 QQ 邮箱');
    $('#account-open').click();
    return;
  }
  state.announcementSelection = new Set();
  $('#announcement-group').value = '';
  $('#announcement-search').value = '';
  $('#announcement-subject').value = '';
  $('#announcement-body').value = '';
  $('#announcement-preview-subject').textContent = '通知主题预览';
  $('#announcement-preview-body').textContent = '填写正文后可在此核对。';
  $('#announcement-result').hidden = true;
  renderAnnouncementRecipients();
  $('#announcement-dialog').showModal();
});

$('#announcement-cancel').addEventListener('click', () => $('#announcement-dialog').close());
$('#announcement-group').addEventListener('change', () => {
  state.announcementSelection.clear();
  renderAnnouncementRecipients();
});
$('#announcement-search').addEventListener('input', renderAnnouncementRecipients);
$('#announcement-recipients').addEventListener('change', event => {
  const checkbox = event.target.closest('[data-announcement-qq]');
  if (!checkbox) return;
  if (checkbox.checked) state.announcementSelection.add(checkbox.dataset.announcementQq);
  else state.announcementSelection.delete(checkbox.dataset.announcementQq);
  updateAnnouncementSelection();
});
$('#announcement-select-visible').addEventListener('click', () => {
  visibleAnnouncementMembers().forEach(member => state.announcementSelection.add(member.qq_number));
  renderAnnouncementRecipients();
});
$('#announcement-subject').addEventListener('input', updateAnnouncementSelection);
$('#announcement-body').addEventListener('input', event => {
  $('#announcement-preview-body').textContent = event.target.value || '填写正文后可在此核对。';
  updateAnnouncementSelection();
});
$('#announcement-subject').addEventListener('input', event => {
  $('#announcement-preview-subject').textContent = event.target.value || '通知主题预览';
});

$('#announcement-form').addEventListener('submit', async event => {
  event.preventDefault();
  const qqNumbers = [...state.announcementSelection];
  if (!qqNumbers.length) return toast('请至少选择一位收件人', true);
  const button = $('#announcement-send');
  button.disabled = true;
  button.textContent = `正在逐封发送 (${qqNumbers.length})…`;
  try {
    const result = await api('/api/announcements', {
      method: 'POST',
      body: {
        subject: $('#announcement-subject').value.trim(),
        body: $('#announcement-body').value.trim(),
        group_filter: $('#announcement-group').value,
        qq_numbers: qqNumbers,
      },
    });
    const sent = new Set(result.sent);
    const errors = new Map(result.errors.map(item => [item.qq_number, item.error]));
    const names = new Map(state.roster.map(member => [member.qq_number, member.name]));
    $('#announcement-result').hidden = false;
    $('#announcement-result').innerHTML = `
      <strong>发送结果：${sent.size} 成功，${errors.size} 失败</strong>
      ${errors.size ? `<ul>${[...errors].map(([qq, error]) => `<li>${esc(names.get(qq) || qq)}（${esc(qq)}）：${esc(error)}</li>`).join('')}</ul>` : '<p>所选通知均已发送。</p>'}`;
    state.announcementSelection.clear();
    await refresh();
    renderAnnouncementRecipients();
    toast(`批量通知完成：${sent.size} 封成功${errors.size ? `，${errors.size} 封失败` : ''}`, errors.size > 0);
  } catch (error) {
    toast(error.message, true);
  } finally {
    updateAnnouncementSelection();
  }
});

$('#announcement-history').addEventListener('click', async event => {
  const button = event.target.closest('[data-announcement-detail]');
  if (!button) return;
  try {
    const data = await api(`/api/announcements/${button.dataset.announcementDetail}`);
    const item = data.announcement;
    $('#announcement-history-title').textContent = item.subject;
    $('#announcement-history-meta').textContent = `${item.created_at} · ${item.group_name || '全部分类'}`;
    $('#announcement-history-body').innerHTML = `<strong>${esc(item.subject)}</strong><p>${esc(item.body)}</p>`;
    $('#announcement-history-recipients').innerHTML = data.recipients.map(member => `
      <tr><td>${esc(member.name)} · ${esc(member.qq_number)}</td><td>${esc(member.recipient)}</td>
      <td><span class="status-pill ${member.status === 'sent' ? 'status-submitted' : 'status-missing'}">${member.status === 'sent' ? '已发送' : member.status === 'pending' ? '处理中断' : '失败'}</span></td>
      <td>${esc(member.error || member.sent_at || '—')}</td></tr>`).join('');
    $('#announcement-history-dialog').showModal();
  } catch (error) { toast(error.message, true); }
});

$('#task-history').addEventListener('click', async event => {
  const button = event.target.closest('[data-load-task]');
  if (!button) return;
  try {
    renderTask(await api(`/api/tasks/${button.dataset.loadTask}`));
    showView('overview');
  } catch (error) { toast(error.message, true); }
});

$$('.nav-item').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
$$('.result-tab').forEach(button => button.addEventListener('click', () => {
  $$('.result-tab').forEach(tab => tab.classList.toggle('active', tab === button));
  $$('.tab-content').forEach(tab => tab.classList.toggle('active', tab.id === `tab-${button.dataset.resultTab}`));
}));

function updateDownloadState() {
  const selected = $$('.file-choice:checked').length;
  $('#download-button').disabled = selected === 0;
  $('#download-button').textContent = selected ? `下载所选文件 (${selected})` : '下载所选文件';
}

$('#files-table').addEventListener('change', event => {
  if (event.target.matches('.file-choice')) updateDownloadState();
});
$('#select-all-files').addEventListener('change', event => {
  $$('.file-choice').forEach(input => { input.checked = event.target.checked; });
  updateDownloadState();
});

$('#download-button').addEventListener('click', async () => {
  if (!state.current) return;
  const ids = $$('.file-choice:checked').map(input => Number(input.value));
  const button = $('#download-button');
  button.disabled = true;
  button.textContent = '下载并校验中…';
  try {
    const result = await api(`/api/tasks/${state.current.task.id}/download`, { method: 'POST', body: { attachment_ids: ids } });
    toast(`已下载并校验 ${result.downloaded.length} 个文件：${result.downloaded[0]?.path || ''}`);
    renderTask(await api(`/api/tasks/${state.current.task.id}`));
  } catch (error) { toast(error.message, true); }
  finally { updateDownloadState(); }
});

$('#desktop-export-button').addEventListener('click', async () => {
  if (!state.current || !state.current.counts.files) return;
  const button = $('#desktop-export-button');
  button.disabled = true;
  button.textContent = '正在导出 PDF…';
  try {
    const result = await api(`/api/tasks/${state.current.task.id}/export-desktop`, { method: 'POST', body: {} });
    toast(`已导出 ${result.files} 份 PDF 和来源清单到桌面文件夹“${result.folder_name}”`);
  } catch (error) { toast(error.message, true); }
  finally {
    button.innerHTML = '↥ 导出 PDF 到桌面';
    button.disabled = !state.current?.counts.files;
  }
});

$('#export-button').addEventListener('click', () => {
  if (!state.current) return;
  const rows = [['QQ号', '学号', '姓名', '班级/小组', '状态', '发件地址', '附件', '接收时间', '邮件主题', 'Message-ID', '匹配方式']];
  for (const member of state.current.members) {
    if (!member.files.length) rows.push([member.qq_number, member.student_id, member.name, member.group_name, member.status === 'missing' ? '未提交' : member.status === 'late' ? '迟交' : '已提交', '', '', '', '', '', '']);
    for (const file of member.files) rows.push([member.qq_number, member.student_id, member.name, member.group_name, member.status === 'missing' ? '未提交' : member.status === 'late' ? '迟交' : '已提交', file.from_addr, file.filename, file.received_at, file.subject, file.source_message_id, file.match_method]);
  }
  for (const message of state.current.unmatched) rows.push(['', '', '', '', '待确认身份', message.from_addr, '', message.received_at, message.subject, message.message_id, message.match_method]);
  downloadText(`${state.current.task.name}-提交统计.csv`, rows.map(row => row.map(csvCell).join(',')).join('\r\n'));
});

function renderReminderDrafts(drafts) {
  state.reminderDrafts = drafts;
  const container = $('#reminder-list');
  container.innerHTML = drafts.length ? drafts.map((draft, index) => `
    <div class="reminder-row">
      <label class="reminder-top"><input type="checkbox" class="reminder-choice" value="${esc(draft.qq_number)}" checked><strong>${esc(draft.name)}</strong><small>${esc(draft.qq_number)} · ${esc(draft.group_name || '未分组')}</small></label>
      <div class="reminder-recipient">收件人：${esc(draft.recipient)}</div>
      <div class="reminder-preview"><strong>${esc(draft.subject)}</strong>\n\n${esc(draft.body)}</div>
    </div>`).join('') : '<div class="reminder-empty">当前没有可提醒成员。未核实邮箱、已提交或已经提醒的成员不会出现在这里。</div>';
  updateReminderCount();
  $('#send-reminders-button').disabled = !drafts.length;
}

function updateReminderCount() {
  const selected = $$('.reminder-choice:checked').length;
  $('#reminder-selection-count').textContent = `已选 ${selected} / ${state.reminderDrafts.length}`;
  $('#send-reminders-button').disabled = selected === 0;
}

$('#reminder-button').addEventListener('click', async () => {
  if (!state.current) return;
  try {
    const data = await api(`/api/tasks/${state.current.task.id}/reminders`);
    renderReminderDrafts(data.drafts);
    $('#reminder-dialog').showModal();
  } catch (error) { toast(error.message, true); }
});

$('#reminder-list').addEventListener('change', event => {
  if (event.target.matches('.reminder-choice')) updateReminderCount();
});

$('#reminder-select-all').addEventListener('change', event => {
  const checked = event.target.checked;
  $$('.reminder-choice').forEach(input => { input.checked = checked; });
  updateReminderCount();
});

$('#send-reminders-button').addEventListener('click', async () => {
  if (!state.current) return;
  const qqNumbers = $$('.reminder-choice:checked').map(input => input.value);
  if (!qqNumbers.length) return;
  const button = $('#send-reminders-button');
  button.disabled = true;
  button.textContent = '正在发送…';
  try {
    const result = await api(`/api/tasks/${state.current.task.id}/remind`, { method: 'POST', body: { qq_numbers: qqNumbers } });
    $('#reminder-dialog').close();
    toast(`已发送 ${result.sent.length} 封提醒${result.errors.length ? `，${result.errors.length} 封失败` : ''}`, result.errors.length > 0);
    renderTask(await api(`/api/tasks/${state.current.task.id}`));
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { button.textContent = '一键发送催促'; }
});

async function init() {
  const today = new Date();
  const date = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  $('#start-date').value = date;
  $('#end-date').value = date;
  $('#start-date').max = date;
  $('#end-date').max = date;
  setAutoRemindHint($('#auto-remind').checked, false);
  try { await refresh(); }
  catch (error) { toast(`本机服务连接失败：${error.message}`, true); }
}

init();
