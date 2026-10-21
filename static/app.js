const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { roster: [], tasks: [], current: null, reminderDrafts: [], toastTimer: null };

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
  $('#account-label').textContent = data.connected ? data.account : '连接邮箱';
  $('#account-dot').classList.toggle('connected', data.connected);
  renderGroupFilter(data.groups);
  renderRoster();
  renderHistory();
}

function renderGroupFilter(groups) {
  const select = $('#group-filter');
  const previous = select.value;
  select.innerHTML = '<option value="">全部在册成员</option>' + groups.map(group => `<option value="${esc(group)}">${esc(group)}</option>`).join('');
  if (groups.includes(previous)) select.value = previous;
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
  try { await refresh(); }
  catch (error) { toast(`本机服务连接失败：${error.message}`, true); }
}

init();
